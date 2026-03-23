# models/order.py
from beanie import Document
from pydantic import BaseModel, Field
from datetime import datetime
from typing import List, Optional
from utils.enums import OrderStatus


# ✅ FIX: OrderItem must be a plain Pydantic BaseModel, NOT a Beanie Document.
# In Beanie 1.x, putting a Document subclass inside another Document's list field
# causes Beanie to treat them as Link references — meaning each OrderItem would need
# its own collection and await insert() call before the parent Order can be saved.
# Using BaseModel embeds them directly as subdocuments in the Order, which is correct.
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
    # ✅ FIX: delivery_fee was missing from the model entirely.
    # get_available_orders and accept_order both do `order.delivery_fee or 15.0`
    # which raised AttributeError, causing a 500 the moment any READY order existed.
    # That crash is why the driver dashboard always showed "no orders available"
    # even when orders had been marked Ready by the admin.
    delivery_fee: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "orders"
