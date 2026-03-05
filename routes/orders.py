from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..dependencies import get_db
from ..schemas.order_schema import OrderCreate
from ..services.order_service import create_order
from ..models.order import Order

router = APIRouter(prefix="/orders")


@router.post("/")
def new_order(data: OrderCreate, db: Session = Depends(get_db)):
    return create_order(db, data)


@router.get("/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db)):
    return db.query(Order).filter(Order.id == order_id).first()
