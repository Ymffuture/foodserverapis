# routes/rewards.py
"""
KotaBites Rewards Wallet — backend for /rewards/*
Replaces all client-side localStorage logic with secure DB storage.

Points economy:
  Earning  : R1 spent (delivered orders) → 0.1 KotaPoint
  Redeem   : 300 kp → R25 | 650 kp → R50 | 1 500 kp → R120
  Tiers    : Bronze 0-499 | Silver 500-1499 | Gold 1500-2999 | Platinum 3000+
"""

import logging
import random
import string
from datetime import datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException

from dependencies import get_current_user, get_current_admin_user
from models.user import User
from models.order import Order
from models.reward_code import RewardCode
from schemas.reward_schema import (
    WalletResponse,
    TierInfo,
    RewardCodeOut,
    ClaimRequest,
    ClaimResponse,
    ValidateRequest,
    ValidateResponse,
    UseCodeRequest,
    UseCodeResponse,
)
from utils.enums import OrderStatus

router = APIRouter(prefix="/rewards", tags=["Rewards"])
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

TIERS: list[dict] = [
    {"name": "Bronze",   "min": 0,    "max": 499,      "color": "#cd7f32", "bg": "rgba(205,127,50,0.12)",  "border": "rgba(205,127,50,0.3)",  "icon": "🥉"},
    {"name": "Silver",   "min": 500,  "max": 1499,     "color": "#94a3b8", "bg": "rgba(148,163,184,0.12)", "border": "rgba(148,163,184,0.3)", "icon": "🥈"},
    {"name": "Gold",     "min": 1500, "max": 2999,     "color": "#FFC72C", "bg": "rgba(255,199,44,0.12)",  "border": "rgba(255,199,44,0.3)",  "icon": "🥇"},
    {"name": "Platinum", "min": 3000, "max": 999_999,  "color": "#60a5fa", "bg": "rgba(96,165,250,0.12)",  "border": "rgba(96,165,250,0.3)",  "icon": "💎"},
]

REDEEM_OPTIONS: dict[int, dict] = {
    300:  {"discount": 25,  "label": "R25 Off"},
    650:  {"discount": 50,  "label": "R50 Off"},
    1500: {"discount": 120, "label": "R120 Off"},
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_tier(pts: int) -> dict:
    for t in TIERS:
        if t["min"] <= pts <= t["max"]:
            return t
    return TIERS[0]


def _get_next_tier(pts: int) -> dict | None:
    for t in TIERS:
        if t["min"] > pts:
            return t
    return None


def _tier_progress(pts: int, tier: dict, next_tier: dict | None) -> int:
    if not next_tier:
        return 100
    span = next_tier["min"] - tier["min"]
    done = pts - tier["min"]
    return min(100, round(done / span * 100)) if span else 100


def _to_tier_info(t: dict) -> TierInfo:
    return TierInfo(**t)


def _generate_code() -> str:
    """Generate secure 24-character code, e.g. KB8F3K9X2L0QW7ZP1R5T6Y"""
    
    chars = string.ascii_uppercase + string.digits
    
    length = 24 - 2  # subtract prefix length ("KB")
    suffix = "".join(secrets.choice(chars) for _ in range(length))
    
    return f"KB{suffix}"


async def _unique_code() -> str:
    """Generate a code that does not already exist in the DB."""
    for _ in range(10):
        code = _generate_code()
        if not await RewardCode.find_one(RewardCode.code == code):
            return code
    raise RuntimeError("Could not generate unique reward code after 10 attempts")


async def _earned_points(user_id: str) -> tuple[int, list[Order]]:
    """Return (earned_kp, delivered_orders) for a user."""
    delivered = await Order.find({
        "user_id": user_id,
        "status": OrderStatus.DELIVERED.value,
    }).to_list()
    total_spend = sum(o.total_amount or 0 for o in delivered)
    return round(total_spend * 0.1), delivered


async def _redeemed_points(user_id: str) -> int:
    """Sum of points_spent across ALL claimed codes (used or not, active or expired)."""
    codes = await RewardCode.find(RewardCode.user_id == user_id).to_list()
    return sum(c.points_spent for c in codes)


def _serialize_code(c: RewardCode) -> RewardCodeOut:
    return RewardCodeOut(
        id=str(c.id),
        code=c.code,
        discount=c.discount,
        points_spent=c.points_spent,
        label=c.label,
        used=c.used,
        used_at=c.used_at,
        expires_at=c.expires_at,
        created_at=c.created_at,
        applied_order_id=c.applied_order_id,
        is_expired=datetime.utcnow() > c.expires_at and not c.used,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/wallet", response_model=WalletResponse)
async def get_wallet(current_user: User = Depends(get_current_user)):
    """
    Return the full wallet state for the authenticated customer.
    All figures are computed server-side from the DB — no client trust.
    """
    uid = str(current_user.id)
    earned, delivered = await _earned_points(uid)
    redeemed          = await _redeemed_points(uid)
    available         = max(0, earned - redeemed)

    tier      = _get_tier(earned)
    next_tier = _get_next_tier(earned)
    progress  = _tier_progress(earned, tier, next_tier)

    codes_raw = await RewardCode.find(
        RewardCode.user_id == uid
    ).sort("-created_at").to_list()

    return WalletResponse(
        earned_points=earned,
        redeemed_points=redeemed,
        available_points=available,
        tier=_to_tier_info(tier),
        next_tier=_to_tier_info(next_tier) if next_tier else None,
        tier_progress=progress,
        order_count=len(delivered),
        codes=[_serialize_code(c) for c in codes_raw],
    )


@router.post("/claim", response_model=ClaimResponse, status_code=201)
async def claim_reward(
    body: ClaimRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Exchange KotaPoints for a reward code.
    - Validates the user has enough available points.
    - Inserts a new RewardCode document.
    - Returns the generated code + updated balance.
    """
    option = REDEEM_OPTIONS.get(body.points)
    if not option:
        valid = list(REDEEM_OPTIONS.keys())
        raise HTTPException(422, f"Invalid points value. Must be one of: {valid}")

    uid               = str(current_user.id)
    earned, _         = await _earned_points(uid)
    redeemed          = await _redeemed_points(uid)
    available         = max(0, earned - redeemed)

    if available < body.points:
        raise HTTPException(400, f"Not enough KotaPoints. Need {body.points}, have {available}.")

    code = await _unique_code()
    rc   = RewardCode(
        user_id=uid,
        code=code,
        discount=option["discount"],
        points_spent=body.points,
        label=option["label"],
    )
    await rc.insert()
    logger.info(f"Reward claimed | user={current_user.email} | code={code} | pts={body.points} | R{option['discount']} off")

    new_available = available - body.points
    return ClaimResponse(
        code=code,
        discount=option["discount"],
        label=option["label"],
        points_spent=body.points,
        expires_at=rc.expires_at,
        available_points=new_available,
    )


@router.post("/validate", response_model=ValidateResponse)
async def validate_code(body: ValidateRequest):
    """
    Checkout calls this before applying a promo code.
    Does NOT mark the code as used — that happens in /use after order creation.
    Public endpoint so Checkout can call it with just the code string.
    """
    code_str = body.code.strip().upper()
    rc = await RewardCode.find_one(RewardCode.code == code_str)

    if not rc:
        return ValidateResponse(valid=False, reason="Code not found")
    if rc.used:
        return ValidateResponse(valid=False, reason="Code has already been used")
    if datetime.utcnow() > rc.expires_at:
        return ValidateResponse(valid=False, reason="Code has expired")

    return ValidateResponse(
        valid=True,
        discount=rc.discount,
        label=rc.label,
        code=rc.code,
    )


@router.post("/use", response_model=UseCodeResponse)
async def use_code(
    body: UseCodeRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Called by Checkout after an order is successfully created.
    Marks the code as used and links it to the order.
    Verifies the code belongs to the authenticated user.
    """
    code_str = body.code.strip().upper()
    rc = await RewardCode.find_one(RewardCode.code == code_str)

    if not rc:
        raise HTTPException(404, "Reward code not found")
    if rc.user_id != str(current_user.id):
        raise HTTPException(403, "This code does not belong to your account")
    if rc.used:
        raise HTTPException(409, "Code has already been used")
    if datetime.utcnow() > rc.expires_at:
        raise HTTPException(410, "Code has expired")

    rc.used             = True
    rc.used_at          = datetime.utcnow()
    rc.applied_order_id = body.order_id
    await rc.save()

    logger.info(f"Reward used | user={current_user.email} | code={code_str} | order={body.order_id} | R{rc.discount} off")
    return UseCodeResponse(
        success=True,
        message=f"{rc.label} applied to order #{body.order_id[-8:].upper()}",
        discount=rc.discount,
    )


# ── Admin ──────────────────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_all_codes(
    limit: int = 200,
    admin: User = Depends(get_current_admin_user),
):
    """Admin: view all reward codes in the system."""
    codes = await RewardCode.find_all().sort("-created_at").limit(limit).to_list()
    return [
        {
            "id":               str(c.id),
            "user_id":          c.user_id,
            "code":             c.code,
            "discount":         c.discount,
            "points_spent":     c.points_spent,
            "label":            c.label,
            "used":             c.used,
            "used_at":          c.used_at,
            "expires_at":       c.expires_at,
            "created_at":       c.created_at,
            "applied_order_id": c.applied_order_id,
            "is_expired":       datetime.utcnow() > c.expires_at and not c.used,
        }
        for c in codes
    ]


@router.get("/admin/user/{user_id}")
async def admin_user_wallet(
    user_id: str,
    admin: User = Depends(get_current_admin_user),
):
    """Admin: inspect any user's wallet (points + codes)."""
    user = await User.get(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    earned, delivered = await _earned_points(user_id)
    redeemed          = await _redeemed_points(user_id)
    codes_raw         = await RewardCode.find(RewardCode.user_id == user_id).sort("-created_at").to_list()

    return {
        "user_id":         user_id,
        "email":           user.email,
        "full_name":       user.full_name,
        "earned_points":   earned,
        "redeemed_points": redeemed,
        "available_points": max(0, earned - redeemed),
        "tier":            _get_tier(earned)["name"],
        "order_count":     len(delivered),
        "codes": [_serialize_code(c).model_dump() for c in codes_raw],
    }
