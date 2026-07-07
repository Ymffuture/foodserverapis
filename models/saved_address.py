# models/saved_address.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import Optional


class SavedAddress(Document):
    user_id: str
    label: str = "Home"           # "Home" | "Work" | "Other" | custom
    address: str
    phone: Optional[str] = None   # optional per-address contact number (e.g. work reception)
    is_default: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "saved_addresses"
        indexes = ["user_id"]
