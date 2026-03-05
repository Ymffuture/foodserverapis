from pydantic import BaseModel

class PaymentInit(BaseModel):
    order_id: int
