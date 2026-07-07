# schemas/order_schema.py
from pydantic import BaseModel, field_serializer, field_validator
from typing import Optional, List
from datetime import datetime, timezone, timedelta


class OrderItemInput(BaseModel):
    menu_item_id: str
    quantity: int


class OrderCreate(BaseModel):
    items: List[OrderItemInput]
    delivery_address: str
    phone: str
    payment_method: Optional[str] = "paystack"
    delivery_fee: Optional[float] = 15.0
    # Reward code discount applied on the frontend.
    # Backend uses this to verify total_amount and stores it on the order
    # so it can be shown on the order status/tracking page.
    discount: Optional[float] = 0.0
    scheduled_for: Optional[datetime] = None  # ← NEW — "order for 6pm"

    @field_validator("scheduled_for")
    @classmethod
    def _validate_scheduled_for(cls, v):
        if v is None:
            return v
        now = datetime.now(timezone.utc)
        target = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if target <= now + timedelta(minutes=20):
            raise ValueError("Scheduled time must be at least 20 minutes from now.")
        if target > now + timedelta(days=7):
            raise ValueError("Orders can only be scheduled up to 7 days ahead.")
        return v


class OrderItemResponse(BaseModel):
    menu_item_id: str
    name: str
    price: float
    quantity: int
    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: Optional[str] = None
    total_amount: float
    status: str
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    created_at: datetime
    delivery_address: str
    phone: Optional[str] = None
    delivery_fee: Optional[float] = None
    discount: Optional[float] = None        # ← reward discount stored on order
    scheduled_for: Optional[datetime] = None  # ← NEW
    items: List[OrderItemResponse] = []
    model_config = {"from_attributes": True}

    @field_serializer("id", when_used="always")
    def serialize_id(self, value):
        return str(value) if value is not None else None
