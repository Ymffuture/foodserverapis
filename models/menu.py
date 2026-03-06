from sqlalchemy import Column, Integer, String, Float
from database import Base

class MenuItem(Base):
    __tablename__ = "menu_items"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String)
    price = Column(Float, nullable=False)
    image_url = Column(String)
    category = Column(String)  # Regular, Deluxe, Cheese, Russian etc.
