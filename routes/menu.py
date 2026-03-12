# routes/menu.py
from fastapi import APIRouter, UploadFile, File, HTTPException, status, Depends
from typing import List, Optional
from bson import ObjectId
from pydantic import ValidationError
from models.menu import MenuItem
from schemas.menu_schema import MenuItemCreate, MenuItemResponse
from services.cloudinary_service import upload_image
from dependencies import get_current_user  # assuming you have admin protection
from models.user import User
import logging

# Setup logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

router = APIRouter(prefix="/menu", tags=["Menu"])


@router.get("/", response_model=List[MenuItemResponse])
async def get_menu():
    """
    Fetch all menu items.
    Returns empty list if none exist (no 404 here).
    """
    try:
        items = await MenuItem.find_all().to_list(length=1000)  # reasonable limit
        logger.info(f"Retrieved {len(items)} menu items")
        return items
    except Exception as e:
        logger.error(f"Error fetching menu items: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while fetching menu items"
        )


@router.get("/{item_id}", response_model=MenuItemResponse)
async def get_menu_item(item_id: str):
    """
    Get a single menu item by ID.
    Validates ObjectId format early.
    """
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
        return item
    except Exception as e:
        logger.error(f"Error fetching menu item {item_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve menu item"
        )


@router.post("/", response_model=MenuItemResponse, status_code=201)
async def create_menu_item(
    name: str,
    price: float,
    category: str,
    description: Optional[str] = None,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)  # ← add admin protection if needed
):
    """
    Create a new menu item with image upload.
    Validates required fields, price > 0, file presence, etc.
    """
    # Basic input validation
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
        # Upload image first — fail fast if Cloudinary fails
        image_url = await upload_image(file)  # assume this is now async

        # Create document
        menu_item = MenuItem(
            name=name.strip(),
            price=price,
            category=category.strip(),
            description=description.strip() if description else None,
            image_url=image_url
        )

        # Insert into MongoDB
        await menu_item.insert()
        logger.info(f"Menu item created: {menu_item.id} - {menu_item.name}")

        return menu_item

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
    current_user: User = Depends(get_current_user)  # ← protect this endpoint
):
    """
    Delete a menu item by ID.
    Returns 204 on success, 404 if not found.
    """
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
        return None  # 204 No Content

    except Exception as e:
        logger.error(f"Error deleting menu item {item_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete menu item"
        )
