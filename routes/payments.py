from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models.order import Order
from schemas.payment_schema import PaymentResponse
from services.paystack_service import initialize_payment, verify_payment
from dependencies import get_current_user
from models.user import User
import uuid

router = APIRouter()

@router.post("/initialize")
def initialize_paystack_payment(order_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.id == order_id, Order.user_id == current_user.id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    reference = str(uuid.uuid4())
    order.payment_reference = reference
    db.commit()

    response = initialize_payment(current_user.email, int(order.total_amount), reference)
    return response

@router.get("/verify/{reference}")
def verify_paystack_payment(reference: str, db: Session = Depends(get_db)):
    result = verify_payment(reference)
    if result.get("status") and result["data"]["status"] == "success":
        order = db.query(Order).filter(Order.payment_reference == reference).first()
        if order:
            order.status = "paid"
            db.commit()
        return {"status": True, "message": "Payment successful"}
    return {"status": False, "message": "Payment failed"}
