# models/reward_code.py
from beanie import Document
from pydantic import Field
from datetime import datetime, timedelta
from typing import Optional


def _default_expires() -> datetime:
    return datetime.utcnow() + timedelta(days=30)


class RewardCode(Document):
    """
    Persists every reward code a customer claims.
    Points are deducted at claim-time (not at checkout),
    matching the original UI behaviour.
    """
    user_id: str
    code: str

    discount: float
    points_spent: int
    label: str

    used: bool = False
    used_at: Optional[datetime] = None
    applied_order_id: Optional[str] = None

    expires_at: datetime = Field(default_factory=_default_expires)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "reward_codes"
        indexes = [
            "user_id",
            # "code" removed — unique index is owned by database.py
            [("user_id", 1), ("used", 1)],
            [("user_id", 1), ("created_at", -1)],
        ]
