# models/user.py
from beanie import Document
from pydantic import EmailStr
from typing import Optional
from datetime import datetime

class User(Document):
    email: EmailStr
    hashed_password: Optional[str] = None   # None = OAuth-only account
    full_name: str
    phone: Optional[str] = None
    # Google OAuth
    google_id:  Optional[str] = None
    picture:    Optional[str] = None
    # GitHub OAuth
    github_id:  Optional[str] = None
    # Spotify OAuth
    spotify_id: Optional[str] = None
    # Email verification
    email_verified:      bool = False
    verification_token:  Optional[str] = None
    # Password reset
    reset_token:         Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    # Admin
    is_admin: bool = False

    class Settings:
        name = "users"
