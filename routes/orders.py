from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from models.order import Order
from schemas.order_schema import OrderCreate, OrderResponse
from services.order_service import create_order
from dependencies import get_current_user
from models.user import User
from typing import List

router = APIRouter()

@router.post("/", response_model=OrderResponse)
def create_new_order(order: OrderCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        new_order = create_order(db, order, current_user.id)
        return new_order
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/me", response_model=List[OrderResponse])
def get_my_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Order).filter(Order.user_id == current_user.id).all()
