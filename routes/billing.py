# routes/billing.py
"""
ProBite subscription billing.

  GET  /billing/plans              – pricing + feature list (frontend pricing page reads this)
  GET  /billing/me                 – current user's plan + bot-credit status
  POST /billing/subscribe          – starts a Paystack checkout for monthly/yearly ProBite
  GET  /billing/verify/{reference} – call after Paystack redirects back; grants access immediately
  POST /billing/cancel             – stop future renewals (stays ProBite until period end)
  POST /billing/webhook            – Paystack server-to-server events (source of truth for renewals)

Flow:
  1. Frontend calls POST /subscribe → gets a Paystack `authorization_url`,
     redirects the customer there.
  2. Paystack redirects back to your frontend's success page with
     ?reference=... → frontend calls GET /verify/{reference} for instant
     "you're ProBite now" UI feedback.
  3. Paystack ALSO calls POST /webhook server-to-server — this is the
     durable source of truth (handles renewals, failed cards, the
     customer closing the tab before step 2, etc).
"""
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dependencies import get_current_user
from models.user import User
from utils.enums import SubscriptionPlan, BillingCycle, SubscriptionStatus
from services import credits_service, paystack_service
from config import (
    PROBITE_PRICE_MONTHLY_ZAR,
    PROBITE_PRICE_YEARLY_ZAR,
    PAYSTACK_PLAN_CODE_MONTHLY,
    PAYSTACK_PLAN_CODE_YEARLY,
)

router = APIRouter(prefix="/billing", tags=["Billing"])
logger = logging.getLogger(__name__)

_PERIOD_DAYS = {BillingCycle.MONTHLY: 30, BillingCycle.YEARLY: 365}
_REFERENCE_PREFIX = "probite"


def _plan_code_for(cycle: BillingCycle) -> Optional[str]:
    return PAYSTACK_PLAN_CODE_MONTHLY if cycle == BillingCycle.MONTHLY else PAYSTACK_PLAN_CODE_YEARLY


def _price_for(cycle: BillingCycle) -> float:
    return PROBITE_PRICE_MONTHLY_ZAR if cycle == BillingCycle.MONTHLY else PROBITE_PRICE_YEARLY_ZAR


def _cycle_from_reference(reference: str) -> Optional[BillingCycle]:
    # references look like "probite-monthly-<uuid hex>" / "probite-yearly-<uuid hex>"
    parts = reference.split("-")
    if len(parts) >= 2 and parts[0] == _REFERENCE_PREFIX:
        try:
            return BillingCycle(parts[1])
        except ValueError:
            return None
    return None


# ============================================================
# Plans (public — no auth needed, the pricing page is pre-login)
# ============================================================

@router.get("/plans")
async def get_plans():
    return {
        "plans": [
            {
                "id": "free",
                "name": "Free",
                "price_monthly": 0,
                "price_yearly": 0,
                "currency": "ZAR",
                "features": [
                    f"{credits_service.FREE_PLAN_CREDIT_CAP} KotaBot credits, refilling every "
                    f"{credits_service.FREE_PLAN_RESET_HOURS} hours",
                    "Like and comment on menu items",
                    "Real-time order tracking",
                    "KotaPoints rewards wallet — earn & redeem on every order",
                    "Passkey / fingerprint sign-in",
                ],
            },
            {
                "id": "probite",
                "name": "ProBite",
                "price_monthly": PROBITE_PRICE_MONTHLY_ZAR,
                "price_yearly": PROBITE_PRICE_YEARLY_ZAR,
                "currency": "ZAR",
                "features": [
                    "Unlimited KotaBot chat — no credits, no waiting",
                    "Edit your comments anytime",
                    "Get notified on likes & replies to your comments",
                    "Everything in Free",
                ],
            },
        ]
    }


# ============================================================
# Current status
# ============================================================

@router.get("/me")
async def get_my_billing(user: User = Depends(get_current_user)):
    credits_status = await credits_service.get_status(user)
    return {
        "plan": user.plan.value,
        "billing_cycle": user.billing_cycle.value if user.billing_cycle else None,
        "subscription_status": user.subscription_status.value,
        "expires_at": user.subscription_expires_at.isoformat() + "Z" if user.subscription_expires_at else None,
        "cancel_at_period_end": user.subscription_cancel_at_period_end,
        "credits": credits_status,
    }


# ============================================================
# Subscribe
# ============================================================

class SubscribeBody(BaseModel):
    billing_cycle: BillingCycle


@router.post("/subscribe")
async def subscribe(
    body: SubscribeBody,
    user: User = Depends(get_current_user),
):
    if user.plan == SubscriptionPlan.PROBITE and not user.subscription_cancel_at_period_end:
        raise HTTPException(400, "You're already on ProBite.")

    plan_code = _plan_code_for(body.billing_cycle)
    if not plan_code:
        # Plan codes are created once on the Paystack dashboard (or via
        # services.paystack_service.create_plan) — this fires if that
        # setup step hasn't happened yet in this environment.
        raise HTTPException(
            503,
            f"ProBite {body.billing_cycle.value} billing isn't configured yet "
            f"— set PAYSTACK_PLAN_CODE_{body.billing_cycle.value.upper()} in .env",
        )

    reference = f"{_REFERENCE_PREFIX}-{body.billing_cycle.value}-{uuid.uuid4().hex}"
    amount = _price_for(body.billing_cycle)

    result = paystack_service.initialize_subscription_payment(
        email=user.email,
        amount=amount,
        reference=reference,
        plan_code=plan_code,
    )

    if not result.get("status"):
        logger.warning(f"Paystack init failed for {user.email}: {result}")
        raise HTTPException(502, "Couldn't start checkout — please try again.")

    return {
        "authorization_url": result["data"]["authorization_url"],
        "reference": reference,
        "amount": amount,
        "currency": "ZAR",
    }


# ============================================================
# Verify (client-side, instant feedback after Paystack redirect)
# ============================================================

async def _grant_probite(user: User, cycle: BillingCycle, paystack_data: dict) -> None:
    now = datetime.utcnow()
    if user.plan != SubscriptionPlan.PROBITE:
        user.subscription_started_at = now

    user.plan = SubscriptionPlan.PROBITE
    user.billing_cycle = cycle
    user.subscription_status = SubscriptionStatus.ACTIVE
    user.subscription_expires_at = now + timedelta(days=_PERIOD_DAYS[cycle])
    user.subscription_cancel_at_period_end = False

    customer = paystack_data.get("customer") or {}
    authorization = paystack_data.get("authorization") or {}
    if customer.get("customer_code"):
        user.paystack_customer_code = customer["customer_code"]
    if authorization.get("authorization_code"):
        user.paystack_authorization_code = authorization["authorization_code"]

    await user.save()


@router.get("/verify/{reference}")
async def verify_subscription(
    reference: str,
    user: User = Depends(get_current_user),
):
    cycle = _cycle_from_reference(reference)
    if not cycle:
        raise HTTPException(400, "Unrecognised reference.")

    result = paystack_service.verify_payment(reference)
    data = result.get("data", {})

    if not (result.get("status") and data.get("status") == "success"):
        return {"status": False, "message": "Payment not successful."}

    await _grant_probite(user, cycle, data)

    return {
        "status": True,
        "message": "Welcome to ProBite! 🎉",
        "plan": "probite",
        "billing_cycle": cycle.value,
        "expires_at": user.subscription_expires_at.isoformat() + "Z",
    }


# ============================================================
# Cancel (stop future renewals, keep access until period end)
# ============================================================

@router.post("/cancel")
async def cancel_subscription(user: User = Depends(get_current_user)):
    if user.plan != SubscriptionPlan.PROBITE:
        raise HTTPException(400, "You're not on ProBite.")

    if user.paystack_subscription_code:
        # email_token isn't stored separately in this minimal version —
        # Paystack also lets customers cancel via the link in their
        # receipt email, which works even without it. Wire up the email
        # token (captured from the subscription.create webhook payload)
        # if you want cancellation to work purely from your own UI.
        try:
            paystack_service.disable_subscription(
                user.paystack_subscription_code, user.paystack_authorization_code or ""
            )
        except Exception:
            logger.exception(f"Paystack disable_subscription failed for {user.email}")

    user.subscription_cancel_at_period_end = True
    user.subscription_status = SubscriptionStatus.CANCELLED
    await user.save()

    return {
        "status": True,
        "message": "Renewal cancelled — you'll keep ProBite until your current period ends.",
        "expires_at": user.subscription_expires_at.isoformat() + "Z" if user.subscription_expires_at else None,
    }


# ============================================================
# Webhook (server-to-server — source of truth for renewals)
# ============================================================

@router.post("/webhook")
async def paystack_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature")

    if not paystack_service.verify_webhook_signature(raw_body, signature):
        logger.warning("Rejected webhook with invalid Paystack signature")
        raise HTTPException(401, "Invalid signature")

    event = await request.json()
    event_type = event.get("event")
    data = event.get("data", {})
    logger.info(f"[paystack webhook] {event_type}")

    if event_type == "charge.success":
        email = (data.get("customer") or {}).get("email")
        if email:
            user = await User.find_one(User.email == email)
            if user:
                # Renewal of an existing ProBite sub uses the cycle already
                # on file (recurring charges don't carry our original
                # "probite-monthly-..." reference). First-time signups are
                # already handled by /verify, so this mainly extends
                # period-end on renewal — safe to run twice (idempotent-ish).
                cycle = user.billing_cycle or _cycle_from_reference(data.get("reference", "")) or BillingCycle.MONTHLY
                await _grant_probite(user, cycle, data)

    elif event_type == "subscription.create":
        email = (data.get("customer") or {}).get("email")
        if email:
            user = await User.find_one(User.email == email)
            if user and data.get("subscription_code"):
                user.paystack_subscription_code = data["subscription_code"]
                await user.save()

    elif event_type in ("subscription.disable", "subscription.not_renew"):
        email = (data.get("customer") or {}).get("email")
        if email:
            user = await User.find_one(User.email == email)
            if user:
                user.subscription_cancel_at_period_end = True
                user.subscription_status = SubscriptionStatus.CANCELLED
                await user.save()

    return {"received": True}
