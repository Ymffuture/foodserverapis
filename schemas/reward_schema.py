# schemas/reward_schema.py
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ── Responses ─────────────────────────────────────────────────────────────

class TierInfo(BaseModel):
    name: str
    color: str
    bg: str
    border: str
    icon: str
    min: int
    max: int


class RewardCodeOut(BaseModel):
    id: str
    code: str
    discount: float
    points_spent: int
    label: str
    used: bool
    used_at: Optional[datetime] = None
    expires_at: datetime
    created_at: datetime
    applied_order_id: Optional[str] = None
    is_expired: bool = False


class WalletResponse(BaseModel):
    earned_points: int
    redeemed_points: int
    available_points: int
    tier: TierInfo
    next_tier: Optional[TierInfo] = None
    tier_progress: int                    # 0-100 %
    order_count: int
    codes: List[RewardCodeOut]


class ClaimRequest(BaseModel):
    points: int = Field(..., gt=0)        # must match a REDEEM_OPTIONS key


class ClaimResponse(BaseModel):
    code: str
    discount: float
    label: str
    points_spent: int
    expires_at: datetime
    available_points: int                 # updated balance after claim


class ValidateRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=20)


class ValidateResponse(BaseModel):
    valid: bool
    discount: Optional[float] = None
    label: Optional[str] = None
    code: Optional[str] = None
    reason: Optional[str] = None         # why invalid


class UseCodeRequest(BaseModel):
    code: str
    order_id: str


class UseCodeResponse(BaseModel):
    success: bool
    message: str
    discount: float
