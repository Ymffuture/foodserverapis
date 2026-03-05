from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..dependencies import get_db
from ..services.paystack_service import *
from ..models.order import Order

router = APIRouter(prefix="/payments")


@router.post("/initialize/{order_id}")
def initialize(order_id: int, db: Session = Depends(get_db)):

    order = db.query(Order).filter(Order.id == order_id).first()

    pay = initialize_payment(order.email, order.total_amount)

    order.payment_reference = pay["data"]["reference"]

    db.commit()

    return pay["data"]


@router.get("/verify/{reference}")
def verify(reference: str, db: Session = Depends(get_db)):

    result = verify_payment(reference)

    order = db.query(Order).filter(
        Order.payment_reference == reference
    ).first()

    if result["data"]["status"] == "success":
        order.order_status = "approved"
    else:
        order.order_status = "declined"

    db.commit()

    return {"status": order.order_status}
