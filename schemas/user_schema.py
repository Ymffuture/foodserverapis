# schemas/user_schema.py
from pydantic import BaseModel, field_validator
from typing import Optional
import re

PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")


class SocialLinksInput(BaseModel):
    facebook:  Optional[str] = None
    github:    Optional[str] = None
    x:         Optional[str] = None
    instagram: Optional[str] = None


class UserProfileUpdate(BaseModel):
    """PATCH /users/me — every field optional so the frontend can send only
    what changed. `None` is left alone; use empty string "" to clear a field."""
    full_name:    Optional[str] = None
    phone:        Optional[str] = None
    address:      Optional[str] = None
    social_links: Optional[SocialLinksInput] = None

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v):
        if v in (None, ""):
            return v
        if not PHONE_RE.match(v.strip()):
            raise ValueError("Enter a valid phone number (7–20 digits, may include +, spaces, -, ()).")
        return v.strip()

    @field_validator("full_name")
    @classmethod
    def _validate_name(cls, v):
        if v is None:
            return v
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Full name must be at least 2 characters.")
        return v


class PasswordChangeRequest(BaseModel):
    current_password: Optional[str] = None  # not required for OAuth-only accounts setting a password for the first time
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError("New password must be at least 8 characters.")
        return v
