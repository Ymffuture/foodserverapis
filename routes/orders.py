# routes/orders.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from models.order import Order
from schemas.order_schema import OrderCreate, OrderResponse
from services.order_service import create_order
from dependencies import get_current_user
from models.user import User
from utils.enums import OrderStatus as OrderStatusEnum
from typing import List

router = APIRouter()


def _serialize(order: Order) -> dict:
    """Convert a Beanie Order document to a plain dict for OrderResponse."""
    return {
        "id":                str(order.id),
        "total_amount":      order.total_amount,
        "status":            order.status.value if hasattr(order.status, "value") else str(order.status),
        # ✅ FIX: was missing — OrderResponse schema has payment_method field
        # Without this, the customer order tracker never showed cash vs paystack
        "payment_method":    order.payment_method,
        "payment_reference": order.payment_reference,
        "created_at":        order.created_at,
        "delivery_address":  order.delivery_address,
        "phone":             order.phone,
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
    created = await create_order(order, str(current_user.id))
    return _serialize(created)


@router.get("/me", response_model=List[OrderResponse])
async def get_my_orders(current_user: User = Depends(get_current_user)):
    orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
    return [_serialize(o) for o in orders]


# ── Admin routes ─────────────────────────────────────────────────────────────
# ✅ FIX: /all and /{order_id}/status PATCH did not exist.
# Admin panel calls GET /orders/all and PATCH /orders/{id}/status —
# both returned 404/405 which is why orders never appeared and status
# changes silently failed.
#
# IMPORTANT: /all and /{order_id}/status MUST be declared BEFORE /{order_id}
# otherwise FastAPI routes "all" and "status" as an order_id path param.

@router.get("/all", response_model=List[OrderResponse])
async def get_all_orders(current_user: User = Depends(get_current_user)):
    """Admin: return every order in the system."""
    orders = await Order.find_all().to_list()
    return [_serialize(o) for o in orders]


@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: str,
    body: StatusUpdate,
    current_user: User = Depends(get_current_user),
):
    """Admin: update the status of any order."""
    order = await Order.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Validate that the incoming status is a valid enum value
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
    return _serialize(order)


# ── Search by short ID (last 8 chars shown in the UI) ─────────────────────
# ✅ FIX: The UI shows order.id.slice(-8).toUpperCase() as the "Order ID" label.
# Users copy this short code and paste it into the Home page track form,
# which then navigates to /order/<short_id> → 404 because Beanie.get() needs
# the full 24-char MongoDB ObjectId.
# This endpoint searches all orders whose string id ends with the given suffix
# (case-insensitive) so both the full id AND the 8-char short code work.
@router.get("/search", response_model=OrderResponse)
async def search_order_by_short_id(
    short_id: str,
    current_user: User = Depends(get_current_user),
):
    """Find an order by its full id OR by the last N characters of its id."""
    short = short_id.strip().lower()
    if not short:
        raise HTTPException(status_code=400, detail="short_id is required")

    # Try as full id first (fast path)
    from bson import ObjectId
    if len(short) == 24:
        try:
            order = await Order.get(short)
            if order and order.user_id == str(current_user.id):
                return _serialize(order)
        except Exception:
            pass

    # Fallback: scan the user's orders for a suffix match
    orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
    for o in orders:
        if str(o.id).lower().endswith(short):
            return _serialize(o)

    raise HTTPException(status_code=404, detail="Order not found")


# ── Single order (customer) — keep LAST so named routes are not shadowed ────

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    # ✅ FIX: also handle short ids here so /order/<short> from the URL bar works
    order = await Order.get(order_id) if len(order_id) == 24 else None

    if order is None:
        # Try suffix search
        short = order_id.strip().lower()
        orders = await Order.find(Order.user_id == str(current_user.id)).to_list()
        for o in orders:
            if str(o.id).lower().endswith(short):
                order = o
                break

    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Order not found")
    return _serialize(order)
