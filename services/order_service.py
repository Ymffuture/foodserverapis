from ..models.order import Order
from ..utils.enums import OrderStatus

def create_order(db, data):

    order = Order(
        customer_name=data.customer_name,
        phone=data.phone,
        email=data.email,
        total_amount=data.total_amount,
        order_status=OrderStatus.submitted.value
    )

    db.add(order)
    db.commit()
    db.refresh(order)

    return order
