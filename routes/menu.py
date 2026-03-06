from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session
from database import get_db
from models.menu import MenuItem
from schemas.menu_schema import MenuItemCreate, MenuItemResponse
from services.cloudinary_service import upload_image
from typing import List

router = APIRouter()

@router.get("/", response_model=List[MenuItemResponse])
def get_menu(db: Session = Depends(get_db)):
    return db.query(MenuItem).all()

@router.post("/", response_model=MenuItemResponse)
def create_menu_item(
    name: str,
    price: float,
    category: str,
    description: str = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    image_url = upload_image(file)
    
    menu_item = MenuItem(
        name=name,
        price=price,
        category=category,
        description=description,
        image_url=image_url
    )
    db.add(menu_item)
    db.commit()
    db.refresh(menu_item)
    return menu_item
