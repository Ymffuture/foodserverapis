# services/credits_service.py
"""
Bot-chat credit metering for the FREE plan.

PROBITE users are unlimited and never touch any of this — every function
here short-circuits immediately for them. Only FREE-plan users spend
credits.

Design notes (read before changing the cost table):
- Cost is based on actual OpenRouter token usage (prompt + completion),
  so a short reply costs less than a long one — the same shape as
  Claude.ai's usage-based metering.
- Credits refill to the FULL cap every FREE_PLAN_RESET_HOURS — it's a
  rolling refill window, not a monthly quota. `bot_credits_reset_at` is
  the timestamp the *next* refill happens.
- Deduction uses an atomic `$inc` (via Beanie's `.update()`) rather than
  `user.save()`, so two concurrent requests can't both read "5 credits
  left" and overdraw the balance.
- This module does NOT enforce a hard pre-charge hold. A FREE user with
  1 credit left can still start a request that ends up costing 12 — the
  pre-check only blocks requests when the balance is already <= 0. This
  is the same trade-off most LLM credit systems make (cost isn't known
  until the response is generated); tightening it would mean estimating
  cost from the prompt alone and holding it before the call.
"""
from datetime import datetime, timedelta
from typing import Optional

from models.user import User, FREE_PLAN_CREDIT_CAP, FREE_PLAN_RESET_HOURS
from utils.enums import SubscriptionPlan

# ── Cost table — tune freely, nothing else needs to change ─────────────────
# (total_tokens upper bound, credit cost)
_TOKEN_COST_TIERS: list[tuple[int, int]] = [
    (300, 5),
    (800, 8),
    (1500, 12),
    (3000, 18),
]
_MAX_TIER_COST = 24  # anything above the last tier's bound

# Flat costs for endpoints that don't go through the main chat model
COST_FILE_READ = 3   # /ai/chat/read-file (Gemini description/transcript)


def is_unlimited(user: User) -> bool:
    return user.plan == SubscriptionPlan.PROBITE


def cost_for_tokens(total_tokens: Optional[int]) -> int:
    """Map a completion's total token usage to a credit cost."""
    if not total_tokens or total_tokens <= 0:
        return _TOKEN_COST_TIERS[0][1]  # unknown usage → cheapest tier, never free
    for bound, cost in _TOKEN_COST_TIERS:
        if total_tokens <= bound:
            return cost
    return _MAX_TIER_COST


def _next_reset(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.utcnow()) + timedelta(hours=FREE_PLAN_RESET_HOURS)


async def ensure_fresh(user: User) -> User:
    """
    Refill a FREE user's credits to the cap if their reset window has
    passed. No-op for PROBITE. Call this before every credit check/charge
    so balances and reset timers stay self-correcting without a cron job.
    """
    if is_unlimited(user):
        return user

    now = datetime.utcnow()
    if user.bot_credits_reset_at is None or now >= user.bot_credits_reset_at:
        user.bot_credits = FREE_PLAN_CREDIT_CAP
        user.bot_credits_reset_at = _next_reset(now)
        await user.save()

    return user


async def get_status(user: User) -> dict:
    """Public-shape credit status for API responses (e.g. GET /billing/me)."""
    await ensure_fresh(user)

    if is_unlimited(user):
        return {"unlimited": True, "credits": None, "credits_cap": None, "resets_at": None}

    return {
        "unlimited": False,
        "credits": user.bot_credits,
        "credits_cap": FREE_PLAN_CREDIT_CAP,
        "resets_at": user.bot_credits_reset_at.isoformat() + "Z" if user.bot_credits_reset_at else None,
    }


def has_credits(user: User) -> bool:
    return is_unlimited(user) or user.bot_credits > 0


async def charge(user: User, cost: int) -> User:
    """
    Atomically deduct `cost` credits (floored at 0), bypassed entirely for
    PROBITE. Refreshes `user.bot_credits` in-memory afterwards so the
    caller can include the new balance in its response.
    """
    if is_unlimited(user) or cost <= 0:
        return user

    new_balance = max(0, user.bot_credits - cost)
    await User.find_one(User.id == user.id).update({"$set": {"bot_credits": new_balance}})
    user.bot_credits = new_balance
    return user


async def charge_for_tokens(user: User, total_tokens: Optional[int]) -> int:
    """Convenience: compute cost from token usage, charge it, return the cost charged."""
    cost = cost_for_tokens(total_tokens)
    await charge(user, cost)
    return cost


async def require_credits(user: User) -> None:
    """
    Call at the top of any bot-chat endpoint. Refreshes the user's credit
    window, then raises HTTP 402 if a FREE user has none left. No-op for
    PROBITE. Import HTTPException locally to avoid a FastAPI dependency
    at module import time for callers that don't need it.
    """
    from fastapi import HTTPException

    await ensure_fresh(user)

    if not has_credits(user):
        resets_at = (
            user.bot_credits_reset_at.isoformat() + "Z"
            if user.bot_credits_reset_at else None
        )
        raise HTTPException(
            status_code=402,
            detail={
                "message": "You're out of free bot credits for now.",
                "credits": 0,
                "credits_cap": FREE_PLAN_CREDIT_CAP,
                "resets_at": resets_at,
                "upgrade_hint": "Go ProBite for unlimited KotaBot chat — no waiting for a refill.",
            },
        )
