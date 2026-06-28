
import hashlib
import hmac
from typing import Optional

import requests
from config import PAYSTACK_SECRET_KEY

HEADERS = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

def initialize_payment(email: str, amount: int, reference: str):
    url = "https://api.paystack.co/transaction/initialize"
    data = {
        "email": email,
        "amount": amount * 100,  # Paystack uses kobo
        "reference": reference,
        "currency": "ZAR"
    }
    response = requests.post(url, json=data, headers=HEADERS)
    return response.json()

def verify_payment(reference: str):
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    response = requests.get(url, headers=HEADERS)
    return response.json()


# ── ProBite subscription billing ────────────────────────────────────────────
# Recurring billing on Paystack is plan-based: a "Plan" (created once, either
# on the Paystack dashboard or via create_plan() below) defines the amount +
# interval. Attaching `plan=<plan_code>` to a normal transaction/initialize
# call makes Paystack auto-create the subscription AND auto-charge renewals
# against the customer's saved card — no separate /subscription call needed.

def create_plan(name: str, amount: int, interval: str) -> dict:
    """
    One-time setup helper — run manually (e.g. from a shell/admin script) to
    create the ProBite monthly/yearly Plans on Paystack, then copy the
    returned `plan_code` into PAYSTACK_PLAN_CODE_MONTHLY / _YEARLY in .env.
    `interval` is one of: "monthly", "annually".
    """
    url = "https://api.paystack.co/plan"
    data = {
        "name": name,
        "amount": amount * 100,  # ZAR → kobo/cents
        "interval": interval,
        "currency": "ZAR",
    }
    response = requests.post(url, json=data, headers=HEADERS)
    return response.json()


def initialize_subscription_payment(email: str, amount: int, reference: str, plan_code: str) -> dict:
    """Same as initialize_payment, but attaches a Plan so Paystack auto-renews."""
    url = "https://api.paystack.co/transaction/initialize"
    data = {
        "email": email,
        "amount": amount * 100,
        "reference": reference,
        "currency": "ZAR",
        "plan": plan_code,
    }
    response = requests.post(url, json=data, headers=HEADERS)
    return response.json()


def disable_subscription(subscription_code: str, email_token: str) -> dict:
    """Cancels future renewals. `email_token` comes from the subscription
    object Paystack returns/sends in webhooks — store it when you first see it."""
    url = "https://api.paystack.co/subscription/disable"
    data = {"code": subscription_code, "token": email_token}
    response = requests.post(url, json=data, headers=HEADERS)
    return response.json()


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Paystack signs every webhook with HMAC-SHA512 of the raw request body,
    using your secret key. Always verify this before trusting a webhook —
    otherwise anyone can POST a fake "payment succeeded" event.
    """
    if not signature_header or not PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"), raw_body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)

