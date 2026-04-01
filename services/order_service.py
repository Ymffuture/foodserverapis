# services/order_service.py
from models.order import Order, OrderItem
from models.menu import MenuItem
from schemas.order_schema import OrderCreate
from utils.enums import OrderStatus
from fastapi import HTTPException
from datetime import datetime


# ── Delivery fee tiers — mirrors Checkout.jsx calcDeliveryFee() ──────────
def _calc_delivery_fee(subtotal: float) -> float:
    """Dynamic delivery fee based on order subtotal (before discount)."""
    if subtotal <= 50:  return 8.0
    if subtotal <= 100: return 12.0
    return 15.0


# ── Payment method max limits ─────────────────────────────────────────────
CASH_MAX = 150.0
CARD_MAX = 250.0


async def create_order(order_data: OrderCreate, user_id: str) -> Order:
    items = []
    subtotal = 0.0

    for item_input in order_data.items:
        menu_item = await MenuItem.get(item_input.menu_item_id)
        if not menu_item:
            raise HTTPException(
                status_code=404,
                detail=f"Menu item '{item_input.menu_item_id}' not found",
            )
        subtotal += menu_item.price * item_input.quantity
        items.append(
            OrderItem(
                menu_item_id=str(menu_item.id),
                name=menu_item.name,
                price=menu_item.price,
                quantity=item_input.quantity,
            )
        )

    # ── Delivery fee ───────────────────────────────────────────────────
    # Use the fee sent by the frontend (already includes any reward overage),
    # but fall back to server-calculated tier if not provided.
    base_fee     = _calc_delivery_fee(subtotal)
    delivery_fee = float(getattr(order_data, "delivery_fee", None) or base_fee)

    # ── Discount (reward code) ─────────────────────────────────────────
    # Cap at subtotal — can't discount more than the items cost.
    # Any excess was already added to delivery_fee by the frontend,
    # so we just apply what it says here.
    raw_discount      = float(getattr(order_data, "discount", None) or 0.0)
    effective_discount = min(raw_discount, subtotal)

    # ── Total ─────────────────────────────────────────────────────────
    total_amount = round(subtotal - effective_discount + delivery_fee, 2)
    total_amount = max(0.0, total_amount)

    # ── Payment method validation ──────────────────────────────────────
    payment_method = order_data.payment_method or "paystack"
    if payment_method == "cash" and total_amount > CASH_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Cash on delivery is only available for orders up to R{CASH_MAX:.0f}. "
                   f"Your total is R{total_amount:.2f}. Please pay online.",
        )
    if payment_method == "paystack" and total_amount > CARD_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Online payment is only available for orders up to R{CARD_MAX:.0f}. "
                   f"Your total is R{total_amount:.2f}. Please call us to arrange your order.",
        )

    order = Order(
        user_id=user_id,
        items=items,
        total_amount=total_amount,
        status=OrderStatus.PENDING,
        payment_method=payment_method,
        delivery_address=order_data.delivery_address,
        phone=order_data.phone,
        delivery_fee=delivery_fee,
        discount=effective_discount if effective_discount > 0 else None,
        created_at=datetime.utcnow(),
    )
    await order.insert()
    return order
