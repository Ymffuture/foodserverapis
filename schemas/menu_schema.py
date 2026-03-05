from pydantic import BaseModel

class MenuCreate(BaseModel):
    name: str
    price: float
    description: str

class MenuResponse(MenuCreate):
    id: int
    image: str | None

    class Config:
        from_attributes = True
