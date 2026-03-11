# routes/auth.py
from fastapi import APIRouter, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Depends
from pydantic import BaseModel, EmailStr
from dependencies import get_password_hash, verify_password, create_access_token
from models.user import User

# No prefix here — main.py sets prefix="/auth"
router = APIRouter()


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: str


class Token(BaseModel):
    access_token: str
    token_type: str


@router.post("/register", status_code=201)
async def register(user: UserCreate):
    existing = await User.find_one(User.email == user.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    new_user = User(
        email=user.email,
        hashed_password=get_password_hash(user.password),
        full_name=user.full_name,
        phone=user.phone,
    )
    await new_user.insert()
    return {"msg": "User created successfully"}


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Standard OAuth2 form login (username = email).
    Supports both the Swagger 'Authorize' button and direct POST with
    application/x-www-form-urlencoded body.
    """
    db_user = await User.find_one(User.email == form_data.username)
    if not db_user or not verify_password(form_data.password, db_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": db_user.email})
    return {"access_token": token, "token_type": "bearer"}
