# schemas/reward_schema.py
from pydantic import BaseModel, Field, validator
from typing import Optional, List
from datetime import datetime
from decimal import Decimal


# ── Base Models (Reusable) ────────────────────────────────────────────────

class TimestampMixin(BaseModel):
    created_at: datetime


class ExpirableMixin(BaseModel):
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at


# ── Tier ──────────────────────────────────────────────────────────────────

class TierInfo(BaseModel):
    name: str
    color: str
    bg: str
    border: str
    icon: str
    min_points: int = Field(..., ge=0)
    max_points: int = Field(..., ge=0)

    @validator("max_points")
    def validate_range(cls, v, values):
        if "min_points" in values and v < values["min_points"]:
            raise ValueError("max_points must be >= min_points")
        return v


# ── Reward Code ───────────────────────────────────────────────────────────

class RewardCodeOut(TimestampMixin, ExpirableMixin):
    id: str
    code: str = Field(..., min_length=4, max_length=30)

    discount: Decimal = Field(..., ge=0, le=100)
    points_spent: int = Field(..., ge=0)

    label: str

    used: bool = False
    used_at: Optional[datetime] = None

    applied_order_id: Optional[str] = None

    # Computed flag exposed safely
    is_expired: bool = False

    @validator("is_expired", always=True)
    def compute_expired(cls, v, values):
        expires_at = values.get("expires_at")
        if expires_at:
            return datetime.utcnow() > expires_at
        return False


# ── Wallet ────────────────────────────────────────────────────────────────

class WalletResponse(BaseModel):
    earned_points: int = Field(..., ge=0)
    redeemed_points: int = Field(..., ge=0)
    available_points: int = Field(..., ge=0)

    tier: TierInfo
    next_tier: Optional[TierInfo] = None

    tier_progress: int = Field(..., ge=0, le=100)

    order_count: int = Field(..., ge=0)

    codes: List[RewardCodeOut] = []


# ── Claim ─────────────────────────────────────────────────────────────────

class ClaimRequest(BaseModel):
    points: int = Field(..., gt=0)


class ClaimResponse(ExpirableMixin):
    code: str
    discount: Decimal = Field(..., ge=0, le=100)
    label: str
    points_spent: int

    available_points: int = Field(..., ge=0)


# ── Validation ────────────────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=30)


class ValidateResponse(BaseModel):
    valid: bool

    discount: Optional[Decimal] = Field(None, ge=0, le=100)
    label: Optional[str] = None
    code: Optional[str] = None

    reason: Optional[str] = None


# ── Use Code ──────────────────────────────────────────────────────────────

class UseCodeRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=30)
    order_id: str


class UseCodeResponse(BaseModel):
    success: bool
    message: str
    discount: Decimal = Field(..., ge=0, le=100)
