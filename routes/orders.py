# routes/orders.py
import asyncio
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from jose import jwt, JWTError
from pydantic import BaseModel
from models.order import Order
from models.delivery_assignment import DeliveryAssignment
from schemas.order_schema import OrderCreate, OrderResponse
from services.order_service import create_order
from dependencies import get_current_user, get_current_admin_user
from models.user import User
from utils.enums import OrderStatus as OrderStatusEnum
from config import SECRET_KEY, ALGORITHM
from typing import List

router = APIRouter()


async def _user_from_token(token: str) -> User | None:
    """Decode a JWT the same way get_current_user does, but callable from a
    query-param token — needed because the browser's native EventSource API
    can't set an Authorization header."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            return None
    except JWTError:
        return None
    return await User.find_one(User.email == email)


def _serialize(order: Order) -> dict:
    """Convert a Beanie Order document to a plain dict for OrderResponse."""
    return {
        "id":                str(order.id),
        "total_amount":      order.total_amount,
        "status":            order.status.value if hasattr(order.status, "value") else str(order.status),
        "payment_method":    order.payment_method,
        "payment_reference": order.payment_reference,
        "created_at":        order.created_at,
        "delivery_address":  order.delivery_address,
        "phone":             order.phone,
        "delivery_fee":      order.delivery_fee,
        "discount":          order.discount,
        "items": [
            {
                "menu_item_id": item.menu_item_id,
                "name":         item.name,
                "price":        item.price,
                "quantity":     item.quantity,
            }
            for item in (order.items or [])
        ],
    }


class StatusUpdate(BaseModel):
    status: str


# ── Customer routes ──────────────────────────────────────────────────────────

@router.post("/", response_model=OrderResponse, status_code=201)
async def create_new_order(
    order: OrderCreate,
    current_user: User = Depends(get_current_user),
):
    created = await create_order(order, current_user)
    return _serialize(created)


@router.get("/me", response_model=List[OrderResponse])
async def get_my_orders(current_user: User = Depends(get_current_user)):
    orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
    return [_serialize(o) for o in orders]


# ── Admin routes ─────────────────────────────────────────────────────────────
# IMPORTANT: /all, /search, and /{order_id}/status MUST be declared BEFORE /{order_id}
# otherwise FastAPI treats "all" and "search" as an order_id path parameter.

@router.get("/all", response_model=List[OrderResponse])
async def get_all_orders(
    # FIX Bug 5 (SECURITY): was get_current_user — any customer could dump every order
    # in the database. Changed to get_current_admin_user.
    admin_user: User = Depends(get_current_admin_user),
):
    """Admin: return every order in the system."""
    orders = await Order.find_all().to_list()
    return [_serialize(o) for o in orders]


@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: str,
    body: StatusUpdate,
    # FIX Bug 6 (SECURITY): was get_current_user — any customer could mark any order
    # as delivered, cancel other users' orders, etc. Changed to get_current_admin_user.
    admin_user: User = Depends(get_current_admin_user),
):
    """Admin: update the status of any order."""
    order = await Order.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        new_status = OrderStatusEnum(body.status)
    except ValueError:
        valid = [s.value for s in OrderStatusEnum]
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status '{body.status}'. Must be one of: {valid}",
        )

    order.status = new_status
    await order.save()

    if new_status == OrderStatusEnum.DELIVERED:
        from services.referral_service import apply_referral_reward_if_eligible
        await apply_referral_reward_if_eligible(order)

    return _serialize(order)


@router.get("/search", response_model=OrderResponse)
async def search_order_by_short_id(
    short_id: str,
    current_user: User = Depends(get_current_user),
):
    """Find an order by its full id OR by the last N characters of its id."""
    short = short_id.strip().lower()
    if not short:
        raise HTTPException(status_code=400, detail="short_id is required")

    from bson import ObjectId
    if len(short) == 24:
        try:
            order = await Order.get(short)
            if order and order.user_id == str(current_user.id):
                return _serialize(order)
        except Exception:
            pass

    orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
    for o in orders:
        if str(o.id).lower().endswith(short):
            return _serialize(o)

    raise HTTPException(status_code=404, detail="Order not found")


# ── Real-time order tracking (SSE) ───────────────────────────────────────────
# Placed before the generic /{order_id} GET so "stream" isn't swallowed as an
# order_id path segment (same reasoning as /search and /all above).

@router.get("/{order_id}/stream")
async def stream_order_status(order_id: str, token: str = Query(...)):
    """
    Server-Sent Events stream for live order status. Pushes an update the
    instant the order or its delivery assignment changes, instead of the
    frontend polling GET /orders/{id} every few seconds.

    EventSource (the browser API for consuming SSE) cannot set an
    Authorization header, so the JWT is passed as a query param here and
    validated the same way get_current_user does.
    """
    user = await _user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    order = await Order.get(order_id)
    if not order or order.user_id != str(user.id):
        raise HTTPException(status_code=404, detail="Order not found.")

    async def event_generator():
        last_snapshot = None
        idle_ticks = 0
        try:
            while True:
                current = await Order.get(order_id)
                if not current:
                    yield f"event: error\ndata: {json.dumps({'detail': 'Order no longer exists.'})}\n\n"
                    break

                assignment = await DeliveryAssignment.find_one(DeliveryAssignment.order_id == order_id)
                status_val = current.status.value if hasattr(current.status, "value") else str(current.status)

                snapshot = (
                    status_val,
                    assignment.status.value if assignment else None,
                    assignment.driver_id if assignment else None,
                )

                if snapshot != last_snapshot:
                    payload = {
                        "id": str(current.id),
                        "status": status_val,
                        "total_amount": current.total_amount,
                        "updated_at": datetime.utcnow().isoformat(),
                        "assignment": None if not assignment else {
                            "status": assignment.status.value if hasattr(assignment.status, "value") else str(assignment.status),
                            "driver_name": assignment.driver_name,
                            "driver_phone": assignment.driver_phone,
                            "accepted_at": assignment.accepted_at.isoformat() if assignment.accepted_at else None,
                            "picked_up_at": assignment.picked_up_at.isoformat() if assignment.picked_up_at else None,
                            "estimated_time": assignment.estimated_time,
                        },
                    }
                    yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    last_snapshot = snapshot
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks % 10 == 0:  # ~30s heartbeat to keep proxies from closing an idle connection
                        yield ": heartbeat\n\n"

                if status_val in ("delivered", "cancelled", "refunded"):
                    yield f"event: done\ndata: {json.dumps({'status': status_val})}\n\n"
                    break

                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass  # client disconnected — nothing to clean up, no background task was spawned

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering so events flush immediately
        },
    )


# ── Single order (customer) — keep LAST so named routes are not shadowed ────

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    order = await Order.get(order_id) if len(order_id) == 24 else None

    if order is None:
        short = order_id.strip().lower()
        orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
        for o in orders:
            if str(o.id).lower().endswith(short):
                order = o
                break

    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Order not found")
    return _serialize(order)
