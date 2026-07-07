# services/order_service.py
from models.order import Order, OrderItem
from models.menu import MenuItem
from models.user import User
from schemas.order_schema import OrderCreate
from utils.enums import OrderStatus, SubscriptionPlan
from fastapi import HTTPException
from datetime import datetime


# ── Delivery fee tiers — mirrors Checkout.jsx calcDeliveryFee() ──────────
def _calc_delivery_fee(subtotal: float) -> float:
    """Dynamic delivery fee based on order subtotal (before discount)."""
    if subtotal <= 50:  return 8.0
    if subtotal <= 100: return 12.0
    return 15.0


# ── Payment method max limits — MUST mirror Checkout.jsx (CASH_MAX/CARD_MAX)
# and routes/ai.py (CASH_MAX_FREE/PROBITE, CARD_MAX_FREE/PROBITE). If any of
# these three drift out of sync, KotaBot/the checkout page will quote a limit
# the backend then rejects (this is exactly what was happening: every plan
# was being charged the FREE-tier R150/R250 limit here, so a R700 ProBite
# order — well under their real R3000 card limit — was hard-rejected).
CASH_MAX_FREE,    CASH_MAX_PROBITE = 150.0, 2000.0
CARD_MAX_FREE,    CARD_MAX_PROBITE = 250.0, 3000.0


async def create_order(order_data: OrderCreate, user: User) -> Order:
    user_id = str(user.id)
    is_probite = user.plan == SubscriptionPlan.PROBITE
    cash_max = CASH_MAX_PROBITE if is_probite else CASH_MAX_FREE
    card_max = CARD_MAX_PROBITE if is_probite else CARD_MAX_FREE

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
    if payment_method == "cash" and total_amount > cash_max:
        raise HTTPException(
            status_code=422,
            detail=f"Cash on delivery is only available for orders up to R{cash_max:.0f}. "
                   f"Your total is R{total_amount:.2f}. Please pay online.",
        )
    if payment_method == "paystack" and total_amount > card_max:
        raise HTTPException(
            status_code=422,
            detail=f"Online payment is only available for orders up to R{card_max:.0f}. "
                   f"Your total is R{total_amount:.2f}. Please call us to arrange your order.",
        )

    order = Order(
        user_id=user_id,
        items=items,
        total_amount=total_amount,
        status=OrderStatus.SCHEDULED if order_data.scheduled_for else OrderStatus.PENDING,
        payment_method=payment_method,
        delivery_address=order_data.delivery_address,
        phone=order_data.phone,
        delivery_fee=delivery_fee,
        discount=effective_discount if effective_discount > 0 else None,
        scheduled_for=order_data.scheduled_for,
        created_at=datetime.utcnow(),
    )
    await order.insert()
    return order
