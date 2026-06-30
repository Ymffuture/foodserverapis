# scripts/reset_credit_timers.py
"""
One-off maintenance script — run manually, once, whenever you change
FREE_PLAN_CREDIT_CAP or FREE_PLAN_RESET_HOURS in models/user.py and want
every existing FREE-plan user's credit timer to pick up the new value
immediately, instead of waiting out their already-scheduled refill.

What it does:
  - Clears bot_credits_reset_at to None for every FREE-plan user.
  - credits_service.ensure_fresh() already treats "no reset time set" as
    "refill right now" — so the very next time each of those users sends
    a chat message (or hits GET /billing/me), they're refilled to the
    *current* FREE_PLAN_CREDIT_CAP and rescheduled using the *current*
    FREE_PLAN_RESET_HOURS. No other code needed — this just clears the
    stale timestamp so that existing logic kicks in.
  - PROBITE users are untouched — they don't use bot_credits at all.

Usage (from the project root, same place you'd run main.py):
    python -m scripts.reset_credit_timers

Safe to run more than once — it's idempotent (clearing an already-empty
field is a no-op for anyone who already refreshed).
"""
import asyncio
import logging

from database import init_db, close_db
from models.user import User
from utils.enums import SubscriptionPlan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    await init_db()

    result = await User.find(
        User.plan == SubscriptionPlan.FREE
    ).update({"$set": {"bot_credits_reset_at": None}})

    # Beanie's bulk update doesn't always return a matched/modified count
    # in every Motor version, so fall back to a manual count if needed.
    affected = getattr(result, "modified_count", None)
    if affected is None:
        affected = await User.find(User.plan == SubscriptionPlan.FREE).count()

    logger.info(
        f"✅ Cleared bot_credits_reset_at for {affected} FREE-plan user(s). "
        f"Each will refill to the current cap on their next request."
    )

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
