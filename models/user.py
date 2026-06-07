# models/user.py
from beanie import Document
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime


class UserWarning(BaseModel):
    """Embedded warning record stored inside the User document."""
    reason: str
    message: Optional[str] = None
    issued_by_id: str
    issued_by_name: str
    issued_at: datetime = Field(default_factory=datetime.utcnow)


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
    # ── Account moderation ─────────────────────────────────────────────
    is_suspended: bool = False
    suspension_reason: Optional[str] = None
    suspended_at:  Optional[datetime] = None
    suspended_until: Optional[datetime] = None   # None = indefinite
    suspended_by:  Optional[str] = None          # admin user_id
    is_banned: bool = False
    banned_reason: Optional[str] = None
    banned_at:  Optional[datetime] = None
    banned_by:  Optional[str] = None             # admin user_id
    # ── Warnings ────────────────────────────────────────────────────────
    warnings: List[UserWarning] = Field(default_factory=list)
    warning_count: int = 0
    # ── Meta ─────────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "users"
