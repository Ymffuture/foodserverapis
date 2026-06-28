# services/subscription_service.py
"""
ProBite subscription lifecycle — kept deliberately tiny.

There's no cron job here. Instead, sync_expiry() is called from
dependencies.get_current_user() on every authenticated request, so an
expired subscriber is downgraded the moment they're next seen — the same
"lazy refresh" pattern services/credits_service.py uses for credit refills.

Renewals are driven by Paystack webhooks (routes/billing.py:webhook),
which push subscription_expires_at forward on each successful recurring
charge. This module only ever moves things in the FREE direction.
"""
from datetime import datetime

from models.user import User
from utils.enums import SubscriptionPlan, SubscriptionStatus


async def sync_expiry(user: User) -> User:
    if user.plan != SubscriptionPlan.PROBITE:
        return user

    if user.subscription_expires_at and datetime.utcnow() > user.subscription_expires_at:
        user.plan = SubscriptionPlan.FREE
        user.subscription_status = SubscriptionStatus.EXPIRED
        user.billing_cycle = None
        user.subscription_cancel_at_period_end = False
        await user.save()

    return user
