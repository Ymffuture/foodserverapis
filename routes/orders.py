# routes/orders.py
from fastapi import APIRouter, Depends, HTTPException
from models.order import Order
from schemas.order_schema import OrderCreate, OrderResponse
from services.order_service import create_order
from dependencies import get_current_user
from models.user import User
from typing import List

router = APIRouter()


@router.post("/", response_model=OrderResponse, status_code=201)
async def create_new_order(
    order: OrderCreate,
    current_user: User = Depends(get_current_user),
):
    return await create_order(order, str(current_user.id))


@router.get("/me", response_model=List[OrderResponse])
async def get_my_orders(current_user: User = Depends(get_current_user)):
    return await Order.find(Order.user_id == str(current_user.id)).to_list()


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    order = await Order.get(order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Order not found")
    return order
