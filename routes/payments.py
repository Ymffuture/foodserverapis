# routes/payments.py
from fastapi import APIRouter, Depends, HTTPException
from models.order import Order
from utils.enums import OrderStatus
from services.paystack_service import initialize_payment, verify_payment
from dependencies import get_current_user
from models.user import User
import uuid

router = APIRouter()


@router.post("/initialize")
async def initialize_paystack_payment(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    order = await Order.get(order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Order not found")

    reference = str(uuid.uuid4())
    order.payment_reference = reference
    await order.save()

    response = initialize_payment(
        email=current_user.email,
        amount=int(order.total_amount),
        reference=reference,
    )
    return response


@router.get("/verify/{reference}")
async def verify_paystack_payment(reference: str):
    result = verify_payment(reference)

    if result.get("status") and result.get("data", {}).get("status") == "success":
        order = await Order.find_one(Order.payment_reference == reference)
        if order:
            order.status = OrderStatus.PAID
            await order.save()
        return {"status": True, "message": "Payment successful"}

    return {"status": False, "message": "Payment failed or not found"}
