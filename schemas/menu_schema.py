# schemas/menu_schema.py
from pydantic import BaseModel
from typing import Optional


class MenuItemBase(BaseModel):
    name: str
    description: Optional[str] = None
    price: float
    category: str


class MenuItemCreate(MenuItemBase):
    pass


class MenuItemResponse(MenuItemBase):
    id: Optional[str] = None
    image_url: Optional[str] = None

    model_config = {"from_attributes": True}
