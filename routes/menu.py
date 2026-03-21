# routes/menu.py
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, status, Depends, Query
from typing import List, Optional
from bson import ObjectId
from pydantic import ValidationError
from models.menu import MenuItem, CATEGORY_EMOJIS
from schemas.menu_schema import MenuItemCreate, MenuItemResponse, CategoryResponse
from services.cloudinary_service import upload_image
from dependencies import get_current_user
from models.user import User
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

router = APIRouter(tags=["Menu"])


@router.get("/categories", response_model=List[CategoryResponse])
async def get_categories():
    """
    Get all categories with item counts and emojis.
    Used by the frontend for the category filter pills.
    """
    try:
        pipeline = [
            {"$match": {"is_available": True}},
            {"$group": {
                "_id": {"$toLower": "$category"},
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        
        results = await MenuItem.aggregate(pipeline).to_list()
        
        categories = []
        for result in results:
            cat_name = result["_id"]
            categories.append(CategoryResponse(
                name=cat_name.capitalize(),
                emoji=CATEGORY_EMOJIS.get(cat_name, "🍽️"),
                count=result["count"]
            ))
        
        # Add "All" category with total count
        total = sum(c.count for c in categories)
        categories.insert(0, CategoryResponse(
            name="All",
            emoji="🍽️",
            count=total
        ))
        
        logger.info(f"Retrieved {len(categories)} categories")
        return categories
        
    except Exception as e:
        logger.error(f"Error fetching categories: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch categories"
        )


@router.get("/", response_model=List[MenuItemResponse])
async def get_menu(
    category: Optional[str] = Query(None, description="Filter by category (case-insensitive)"),
    search: Optional[str] = Query(None, description="Search by name or description"),
    available_only: bool = Query(True, description="Only return available items")
):
    """
    Get menu items with optional filtering by category and search query.
    Supports the frontend category pills and search functionality.
    """
    try:
        # Build query dynamically
        query = {}
        
        if available_only:
            query["is_available"] = True
            
        if category and category.lower() != "all":
            # Case-insensitive category match
            query["category"] = {"$regex": f"^{category}$", "$options": "i"}
            
        if search:
            # Search in name and description (case-insensitive)
            regex = {"$regex": search, "$options": "i"}
            query["$or"] = [
                {"name": regex},
                {"description": regex}
            ]
        
        # Fetch items with sorting
        items = await MenuItem.find(query).sort(
            [("category", 1), ("name", 1)]
        ).to_list(length=1000)
        
        logger.info(f"Retrieved {len(items)} menu items (category={category}, search={search})")
        
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
    name: str = Form(...),
    price: float = Form(...),
    category: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Create new menu item with image upload.
    Category is normalized to lowercase for consistent filtering.
    """
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
    if not category.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category cannot be empty"
        )
    if file is None or file.filename == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Image file is required"
        )

    try:
        image_url = await upload_image(file)

        # Normalize category to lowercase for consistent filtering
        normalized_category = category.strip().lower()

        menu_item = MenuItem(
            name=name.strip(),
            price=price,
            category=normalized_category,  # Store lowercase
            description=description.strip() if description else None,
            image_url=image_url,
            is_available=True
        )
        await menu_item.insert()
        logger.info(f"Menu item created: {menu_item.id} - {menu_item.name} (category: {normalized_category})")

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


@router.put("/{item_id}", response_model=MenuItemResponse)
async def update_menu_item(
    item_id: str,
    name: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    category: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    is_available: Optional[bool] = Form(None),
    current_user: User = Depends(get_current_user)
):
    """
    Update existing menu item. All fields optional.
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
        
        # Update fields if provided
        if name is not None:
            item.name = name.strip()
        if price is not None:
            if price <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Price must be greater than zero"
                )
            item.price = price
        if category is not None:
            item.category = category.strip().lower()  # Normalize to lowercase
        if description is not None:
            item.description = description.strip() if description else None
        if is_available is not None:
            item.is_available = is_available
            
        # Handle image update
        if file and file.filename:
            image_url = await upload_image(file)
            item.image_url = image_url
            
        await item.save()
        logger.info(f"Menu item updated: {item_id}")
        
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
        logger.error(f"Error updating menu item {item_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update menu item"
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
