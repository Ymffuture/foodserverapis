from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

class OrderItem(BaseModel):
    menu_item_id: int
    quantity: int

class OrderCreate(BaseModel):
    items: List[OrderItem]
    delivery_address: str
    phone: str

class OrderResponse(BaseModel):
    id: int
    total_amount: float
    status: str
    payment_reference: Optional[str]
    created_at: str
    delivery_address: str

    class Config:
        from_attributes = True
