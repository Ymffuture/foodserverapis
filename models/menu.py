from sqlalchemy import Column, Integer, String, Float
from ..database import Base

class Menu(Base):
    __tablename__ = "menu"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    price = Column(Float)
    image = Column(String)
    description = Column(String)
