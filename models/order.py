# models/order.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import List, Optional
from utils.enums import OrderStatus


class OrderItem(Document):
    menu_item_id: str
    name: str
    price: float
    quantity: int

    class Settings:
        is_root = True


class Order(Document):
    user_id: str
    items: List[OrderItem] = []
    total_amount: float
    status: OrderStatus = OrderStatus.PENDING
    payment_method: Optional[str] = "paystack"   # "cash" | "paystack"
    payment_reference: Optional[str] = None
    delivery_address: str
    phone: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "orders"
