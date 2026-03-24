# services/order_service.py
from models.order import Order, OrderItem
from models.menu import MenuItem
from schemas.order_schema import OrderCreate
from utils.enums import OrderStatus
from fastapi import HTTPException
from datetime import datetime

# ✅ FIX: delivery_fee is now read from OrderCreate and stored on the Order.
# Previously this field was never written, so Order.delivery_fee was always None.
# The delivery route did `order.delivery_fee or 15.0` as a fallback, which worked
# for display, but the admin Orders.jsx column showed "R0.00 delivery" because
# the serialiser sent null. More critically, old orders had no delivery_fee and
# that fallback was the only thing keeping the driver payout correct.

DELIVERY_FEE = 15.0   # default if frontend doesn't send one


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

    # Use the delivery_fee sent by the frontend (R15), falling back to the constant
    delivery_fee = getattr(order_data, "delivery_fee", None) or DELIVERY_FEE
    total_amount = round(subtotal + delivery_fee, 2)

    order = Order(
        user_id=user_id,
        items=items,
        total_amount=total_amount,
        status=OrderStatus.PENDING,
        payment_method=order_data.payment_method or "paystack",
        delivery_address=order_data.delivery_address,
        phone=order_data.phone,
        delivery_fee=delivery_fee,
        created_at=datetime.utcnow(),
    )
    await order.insert()
    return order
