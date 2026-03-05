from pydantic import BaseModel, EmailStr

class OrderCreate(BaseModel):
    customer_name: str
    phone: str
    email: EmailStr
    total_amount: float

class OrderResponse(OrderCreate):
    id: int
    order_status: str
    payment_reference: str | None

    class Config:
        from_attributes = True
