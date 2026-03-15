# models/suggestion.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import Optional


class Suggestion(Document):
    user_id: str
    user_email: str
    message: str
    category: Optional[str] = "general"   # "food", "service", "app", "general"
    sentiment: Optional[str] = None        # "positive", "neutral", "negative" — filled by AI
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "suggestions"
