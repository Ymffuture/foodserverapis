# schemas/order_schema.py
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class OrderItemInput(BaseModel):
    menu_item_id: str
    quantity: int


class OrderCreate(BaseModel):
    items: List[OrderItemInput]
    delivery_address: str
    phone: str


class OrderItemResponse(BaseModel):
    menu_item_id: str
    name: str
    price: float
    quantity: int


class OrderResponse(BaseModel):
    id: Optional[str] = None
    total_amount: float
    status: str
    payment_reference: Optional[str] = None
    created_at: datetime
    delivery_address: str
    items: List[OrderItemResponse] = []

    model_config = {"from_attributes": True}
