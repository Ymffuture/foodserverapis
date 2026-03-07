from beanie import Document
from pydantic import Field

class MenuItem(Document):
    name: str
    description: str = None
    price: float
    image_url: str = None
    category: str

    class Settings:
        name = "menu_items"
