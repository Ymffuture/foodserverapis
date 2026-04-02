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
    user_id: str                          # User who owns this code
    code: str                             # e.g. "KBXYZ123"  – unique

    # Reward details
    discount: float                       # Rand value: 25 | 50 | 120
    points_spent: int                     # KotaPoints deducted at claim
    label: str                            # "R25 Off" | "R50 Off" | "R120 Off"

    # Lifecycle
    used: bool = False
    used_at: Optional[datetime] = None
    applied_order_id: Optional[str] = None   # Order the code was redeemed on

    # Validity window
    expires_at: datetime = Field(default_factory=_default_expires)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "reward_codes"
        indexes = [
            "user_id",
            "code",                       # unique index set in migration
            [("user_id", 1), ("used", 1)],
            [("user_id", 1), ("created_at", -1)],
        ]
