from beanie import Document, Link
from pydantic import Field
from datetime import datetime
from utils.enums import OrderStatus

class Order(Document):
    user_id: str
    total_amount: float
    status: OrderStatus = OrderStatus.PENDING
    payment_reference: str = None
    delivery_address: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "orders"
