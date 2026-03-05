from sqlalchemy import Column, Integer, String, Float
from ..database import Base

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    customer_name = Column(String)
    phone = Column(String)
    email = Column(String)
    total_amount = Column(Float)
    order_status = Column(String, default="submitted")
    payment_reference = Column(String, nullable=True)
