# routes/menu.py
from fastapi import APIRouter, UploadFile, File, HTTPException
from models.menu import MenuItem
from schemas.menu_schema import MenuItemCreate, MenuItemResponse
from services.cloudinary_service import upload_image
from typing import List

router = APIRouter()


@router.get("/", response_model=List[MenuItemResponse])
async def get_menu():
    items = await MenuItem.find_all().to_list()
    return items


@router.get("/{item_id}", response_model=MenuItemResponse)
async def get_menu_item(item_id: str):
    item = await MenuItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    return item


@router.post("/", response_model=MenuItemResponse, status_code=201)
async def create_menu_item(
    name: str,
    price: float,
    category: str,
    description: str = None,
    file: UploadFile = File(...),
):
    image_url = upload_image(file)

    menu_item = MenuItem(
        name=name,
        price=price,
        category=category,
        description=description,
        image_url=image_url,
    )
    await menu_item.insert()
    return menu_item


@router.delete("/{item_id}", status_code=204)
async def delete_menu_item(item_id: str):
    item = await MenuItem.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Menu item not found")
    await item.delete()
