# schemas/menu_schema.py
from pydantic import BaseModel, Field
from typing import Optional


class MenuItemBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price: float = Field(..., gt=0)
    category: str = Field(..., min_length=1, max_length=50)


class MenuItemCreate(MenuItemBase):
    pass


class MenuItemResponse(MenuItemBase):
    id: str
    image_url: Optional[str] = None
    
    class Config:
        from_attributes = True


class CategoryResponse(BaseModel):
    name: str
    emoji: str
    count: int
