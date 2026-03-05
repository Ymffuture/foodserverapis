from fastapi import APIRouter, Depends, UploadFile, File
from sqlalchemy.orm import Session
from ..dependencies import get_db
from ..models.menu import Menu
from ..services.cloudinary_service import upload_image

router = APIRouter(prefix="/menu")

@router.get("/")
def get_menu(db: Session = Depends(get_db)):
    return db.query(Menu).all()


@router.post("/")
def create_menu(
    name: str,
    price: float,
    description: str,
    image: UploadFile = File(...),
    db: Session = Depends(get_db)
):

    image_url = upload_image(image.file)

    item = Menu(
        name=name,
        price=price,
        description=description,
        image=image_url
    )

    db.add(item)
    db.commit()
    db.refresh(item)

    return item
