# models/menu.py
from beanie import Document
from pydantic import Field
from typing import Optional
from datetime import datetime


# Category emoji mapping
CATEGORY_EMOJIS = {
    "kota": "🥪",
    "drinks": "🥤", 
    "sides": "🍟",
    "combos": "🔥",
    "desserts": "🍰",
    "specials": "⭐",
    "all": "🍽️"
}


class MenuItem(Document):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    price: float = Field(..., gt=0)
    image_url: Optional[str] = None
    category: str = Field(..., min_length=1, max_length=50)
    is_available: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "menu_items"
        indexes = [
            "category",
            "name",
            "is_available",
            [("category", 1), ("name", 1)],  # Compound index for sorting
            [("is_available", 1), ("category", 1)]  # For filtered queries
        ]
