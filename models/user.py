# models/user.py
from beanie import Document
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime

from utils.enums import SubscriptionPlan, BillingCycle, SubscriptionStatus

# ── Bot-credit defaults (FREE plan) ─────────────────────────────────────────
# Kept here (not in config.py) since they describe a *User* default, not a
# secret/env value. Change freely — every free user reads these live.
FREE_PLAN_CREDIT_CAP   = 100   # credits a free user holds at full refill
FREE_PLAN_RESET_HOURS  = 2    # credits fully refill this often


class UserWarning(BaseModel):
    """Embedded warning record stored inside the User document."""
    reason: str
    message: Optional[str] = None
    issued_by_id: str
    issued_by_name: str
    issued_at: datetime = Field(default_factory=datetime.utcnow)


class User(Document):
    email: EmailStr
    hashed_password: Optional[str] = None   # None = OAuth-only account
    full_name: str
    phone: Optional[str] = None
    # Google OAuth
    google_id:  Optional[str] = None
    picture:    Optional[str] = None
    # GitHub OAuth
    github_id:  Optional[str] = None
    # Spotify OAuth
    spotify_id: Optional[str] = None
    # Email verification
    email_verified:      bool = False
    verification_token:  Optional[str] = None
    # Password reset
    reset_token:         Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    # Admin
    is_admin: bool = False
    # ── Account moderation ─────────────────────────────────────────────
    is_suspended: bool = False
    suspension_reason: Optional[str] = None
    suspended_at:  Optional[datetime] = None
    suspended_until: Optional[datetime] = None   # None = indefinite
    suspended_by:  Optional[str] = None          # admin user_id
    is_banned: bool = False
    banned_reason: Optional[str] = None
    banned_at:  Optional[datetime] = None
    banned_by:  Optional[str] = None             # admin user_id
    # ── Warnings ────────────────────────────────────────────────────────
    warnings: List[UserWarning] = Field(default_factory=list)
    warning_count: int = 0
    # ── Admin internal note (shown only in admin panel + KotaBot context) ─
    # Referenced by routes/ai.py _build_account_status_block() and
    # routes/Users.py _derive_status(). Stored here so the AI system prompt
    # can surface it without an extra DB query.
    admin_note: Optional[str] = None             # ← NEW
    # ── Meta ─────────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # ── ProBite subscription ────────────────────────────────────────────
    plan: SubscriptionPlan = SubscriptionPlan.FREE
    subscription_status: SubscriptionStatus = SubscriptionStatus.NONE
    billing_cycle: Optional[BillingCycle] = None        # set only when plan == PROBITE
    subscription_started_at: Optional[datetime] = None
    subscription_expires_at: Optional[datetime] = None  # end of current paid period
    subscription_cancel_at_period_end: bool = False     # user hit "cancel" — still PROBITE until expires_at

    # Paystack refs (recurring billing) — never exposed to the frontend
    paystack_customer_code: Optional[str] = None
    paystack_authorization_code: Optional[str] = None    # reusable card token for renewals
    paystack_subscription_code: Optional[str] = None

    # ── Bot chat credits (FREE plan only — PROBITE = unlimited) ──────────
    bot_credits: int = FREE_PLAN_CREDIT_CAP
    bot_credits_reset_at: Optional[datetime] = None     # next time credits refill to the cap

    class Settings:
        name = "users"
