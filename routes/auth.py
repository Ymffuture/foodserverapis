# routes/auth.py
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from passlib.context import CryptContext
from datetime import datetime, timedelta
from jose import jwt
from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from models.user import User
from dependencies import get_password_hash, verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["Auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str
    phone: str


class UserLogin(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str


@router.post("/register", status_code=201)
async def register(user: UserCreate):
    # Check if user already exists
    existing = await User.find_one(User.email == user.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    # Hash password
    hashed = get_password_hash(user.password)

    # Create new user document
    new_user = User(
        email=user.email,
        hashed_password=hashed,
        full_name=user.full_name,
        phone=user.phone
    )

    # Save to MongoDB
    await new_user.insert()

    return {"msg": "User created successfully"}


@router.post("/login", response_model=Token)
async def login(user: UserLogin):
    # Find user by email
    db_user = await User.find_one(User.email == user.email)

    if not db_user or not verify_password(user.password, db_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create JWT
    token = create_access_token({"sub": db_user.email})

    return {"access_token": token, "token_type": "bearer"}
