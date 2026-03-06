from sqlalchemy.orm import Session
from models.order import Order
from models.menu import MenuItem
from schemas.order_schema import OrderCreate
from utils.enums import OrderStatus
from datetime import datetime

def create_order(db: Session, order_data: OrderCreate, user_id: int):
    total = 0.0
    for item in order_data.items:
        menu_item = db.query(MenuItem).filter(MenuItem.id == item.menu_item_id).first()
        if not menu_item:
            raise ValueError(f"Menu item {item.menu_item_id} not found")
        total += menu_item.price * item.quantity

    new_order = Order(
        user_id=user_id,
        total_amount=total,
        status=OrderStatus.PENDING,
        payment_reference=None,
        created_at=datetime.utcnow().isoformat(),
        delivery_address=order_data.delivery_address
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)
    return new_order
