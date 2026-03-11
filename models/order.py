# models/order.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import List
from utils.enums import OrderStatus


class OrderItem(Document):
    menu_item_id: str
    name: str
    price: float
    quantity: int

    class Settings:
        # Embedded, not a top-level collection
        is_root = True


class Order(Document):
    user_id: str
    items: List[OrderItem] = []
    total_amount: float
    status: OrderStatus = OrderStatus.PENDING
    payment_reference: str = None
    delivery_address: str
    phone: str = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "orders"
