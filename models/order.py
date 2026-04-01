# models/order.py
from beanie import Document
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional
from utils.enums import OrderStatus


class OrderItem(BaseModel):
    menu_item_id: str
    name: str
    price: float
    quantity: int


class Order(Document):
    user_id: str
    items: List[OrderItem] = []
    total_amount: float
    status: OrderStatus = OrderStatus.PENDING
    payment_method: Optional[str] = "paystack"   # "cash" | "paystack"
    payment_reference: Optional[str] = None
    delivery_address: str
    phone: Optional[str] = None
    delivery_fee: Optional[float] = None
    # Discount applied at checkout (from reward code).
    # Stored so OrderStatus page and KotaBot can reference the actual discount.
    discount: Optional[float] = Field(default=0.0, ge=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "orders"
