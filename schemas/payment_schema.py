from pydantic import BaseModel
from typing import Optional

class PaymentInitialize(BaseModel):
    order_id: int
    email: str
    amount: float

class PaymentVerify(BaseModel):
    reference: str

class PaymentResponse(BaseModel):
    status: bool
    message: str
    reference: str
    amount: Optional[float]
    gateway_response: Optional[str]
