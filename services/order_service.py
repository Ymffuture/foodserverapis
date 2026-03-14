# services/order_service.py
from models.order import Order, OrderItem
from models.menu import MenuItem
from schemas.order_schema import OrderCreate
from utils.enums import OrderStatus
from fastapi import HTTPException
from datetime import datetime


async def create_order(order_data: OrderCreate, user_id: str) -> Order:
    items = []
    total = 0.0

    for item_input in order_data.items:
        menu_item = await MenuItem.get(item_input.menu_item_id)
        if not menu_item:
            raise HTTPException(
                status_code=404,
                detail=f"Menu item '{item_input.menu_item_id}' not found",
            )
        subtotal = menu_item.price * item_input.quantity
        total += subtotal

        # ✅ OrderItem is now a plain Pydantic BaseModel — no insert() needed.
        # It gets embedded directly as a subdocument inside the Order.
        items.append(
            OrderItem(
                menu_item_id=str(menu_item.id),
                name=menu_item.name,
                price=menu_item.price,
                quantity=item_input.quantity,
            )
        )

    order = Order(
        user_id=user_id,
        items=items,
        total_amount=round(total, 2),
        status=OrderStatus.PENDING,
        payment_method=order_data.payment_method or "paystack",
        delivery_address=order_data.delivery_address,
        phone=order_data.phone,
        created_at=datetime.utcnow(),
    )
    await order.insert()
    return order
