# routes/menu.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status, Depends
from typing import List, Optional
from bson import ObjectId
from pydantic import ValidationError
from models.menu import MenuItem
from schemas.menu_schema import MenuItemCreate, MenuItemResponse
from services.cloudinary_service import upload_image
from dependencies import get_current_user
from models.user import User
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

router = APIRouter(tags=["Menu"])


@router.get("/", response_model=List[MenuItemResponse])
async def get_menu():
    try:
        items = await MenuItem.find_all().to_list(length=1000)
        logger.info(f"Retrieved {len(items)} menu items")
        return [MenuItemResponse(
            id=str(item.id),
            name=item.name,
            description=item.description,
            price=item.price,
            category=item.category,
            image_url=item.image_url,
        ) for item in items]
    except Exception as e:
        logger.error(f"Error fetching menu items: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching menu items"
        )


@router.get("/{item_id}", response_model=MenuItemResponse)
async def get_menu_item(item_id: str):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid menu item ID format"
        )
    try:
        item = await MenuItem.get(item_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Menu item not found"
            )
        return MenuItemResponse(
            id=str(item.id),
            name=item.name,
            description=item.description,
            price=item.price,
            category=item.category,
            image_url=item.image_url,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching menu item {item_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve menu item"
        )


@router.post("/", response_model=MenuItemResponse, status_code=201)
async def create_menu_item(
    # ✅ FIX: Declare all text fields with Form(...) so FastAPI reads them
    # from the multipart/form-data body instead of query parameters.
    # Without Form(...), FastAPI treats them as query params and they
    # never arrive → 422 Unprocessable Entity every time.
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    if price <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Price must be greater than zero"
        )
    if not name.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name cannot be empty"
        )
    if file is None or file.filename == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image file is required"
        )

    try:
        image_url = await upload_image(file)

        menu_item = MenuItem(
            name=name.strip(),
            price=price,
            category=category.strip(),
            description=description.strip() if description else None,
            image_url=image_url
        )
        await menu_item.insert()
        logger.info(f"Menu item created: {menu_item.id} - {menu_item.name}")

        return MenuItemResponse(
            id=str(menu_item.id),
            name=menu_item.name,
            description=menu_item.description,
            price=menu_item.price,
            category=menu_item.category,
            image_url=menu_item.image_url,
        )

    except ValidationError as ve:
        logger.warning(f"Validation error creating menu item: {ve.errors()}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ve.errors()
        )
    except Exception as e:
        logger.error(f"Failed to create menu item: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create menu item due to server error"
        )


@router.delete("/{item_id}", status_code=204)
async def delete_menu_item(
    item_id: str,
    current_user: User = Depends(get_current_user)
):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid menu item ID format"
        )
    try:
        item = await MenuItem.get(item_id)
        if not item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Menu item not found"
            )
        await item.delete()
        logger.info(f"Menu item deleted: {item_id} by user {current_user.email}")
        return None
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting menu item {item_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete menu item"
        )
