# routes/orders.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from models.order import Order
from schemas.order_schema import OrderCreate, OrderResponse
from services.order_service import create_order
from dependencies import get_current_user, get_current_admin_user
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
    created = await create_order(order, str(current_user.id))
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
