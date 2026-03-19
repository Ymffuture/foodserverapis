# routes/auth.py
import os
import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

from dependencies import (
    get_password_hash, verify_password,
    create_access_token, get_current_user,
)
from models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

RESET_EXPIRY_MINUTES = 30
RATE_LIMIT_SECONDS   = 60

# Simple in-memory rate-limit stores (resets on server restart — fine for most use-cases)
_reset_rate:  dict[str, datetime] = {}
_verify_rate: dict[str, datetime] = {}


# ── Schemas ──────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: str

class Token(BaseModel):
    access_token: str
    token_type: str

class GoogleBody(BaseModel):
    access_token: str

class ForgotBody(BaseModel):
    email: EmailStr

class ResetBody(BaseModel):
    token: str
    new_password: str

class VerifyBody(BaseModel):
    token: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_rate(store: dict, key: str) -> None:
    now = datetime.utcnow()
    if key in store and (now - store[key]).total_seconds() < RATE_LIMIT_SECONDS:
        raise HTTPException(429, "Please wait a moment before trying again.")
    store[key] = now


# ── Standard auth ─────────────────────────────────────────────────────────────

@router.post("/register", status_code=201)
async def register(user: UserCreate):
    if await User.find_one(User.email == user.email):
        raise HTTPException(400, "Email already registered")

    token = secrets.token_urlsafe(32)

    await User(
        email=user.email,
        hashed_password=get_password_hash(user.password),
        full_name=user.full_name,
        phone=user.phone,
        email_verified=False,
        verification_token=token,
    ).insert()

    return {
        "msg":       "User created successfully",
        "token":     token,
        "email":     user.email,
        "full_name": user.full_name,
    }


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await User.find_one(User.email == form_data.username)
    if not user or not user.hashed_password or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": create_access_token({"sub": user.email}), "token_type": "bearer"}


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.post("/google")
async def google_login(body: GoogleBody):
    """Verify Google access_token with Google's userinfo endpoint, then upsert user."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {body.access_token}"},
        )
    if r.status_code != 200:
        raise HTTPException(401, "Invalid Google token")

    info = r.json()
    email = info.get("email")
    if not email:
        raise HTTPException(400, "Google account has no email address")

    user = await User.find_one(User.email == email)
    if not user:
        user = User(
            email=email,
            hashed_password=None,
            full_name=info.get("name") or email.split("@")[0],
            phone=None,
            google_id=info.get("sub"),
            picture=info.get("picture"),
            email_verified=bool(info.get("email_verified", True)),
        )
        await user.insert()
        logger.info(f"New Google user created: {email}")
    else:
        changed = False
        if not user.google_id:
            user.google_id = info.get("sub"); changed = True
        if info.get("picture") and user.picture != info.get("picture"):
            user.picture = info.get("picture"); changed = True
        if not user.email_verified:
            user.email_verified = True; changed = True
        if changed:
            await user.save()

    jwt = create_access_token({"sub": user.email})
    return {
        "access_token": jwt,
        "token_type":   "bearer",
        "user": {
            "email":     user.email,
            "full_name": user.full_name,
            "picture":   user.picture or "",
        },
    }


# ── Password Reset ────────────────────────────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(body: ForgotBody):
    """
    Generates a reset token and returns it to the frontend.
    The frontend then uses EmailJS to send the email — no SMTP needed.
    Rate-limited to 1 request per minute per email.
    """
    _check_rate(_reset_rate, body.email)

    user = await User.find_one(User.email == body.email)
    # Always return 200 to prevent email enumeration
    if not user:
        return {"msg": "If that email is registered, a reset link will be sent."}
    if not user.hashed_password:
        raise HTTPException(400, "This account uses Google sign-in — no password to reset.")

    token = secrets.token_urlsafe(32)
    user.reset_token         = token
    user.reset_token_expires = datetime.utcnow() + timedelta(minutes=RESET_EXPIRY_MINUTES)
    await user.save()

    return {
        "msg":        "Token ready for EmailJS",
        "token":      token,
        "email":      user.email,
        "full_name":  user.full_name,
        "expires_in": RESET_EXPIRY_MINUTES,
    }


@router.post("/reset-password")
async def reset_password(body: ResetBody):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    user = await User.find_one(User.reset_token == body.token)
    if not user:
        raise HTTPException(400, "Invalid or expired reset token")
    if not user.reset_token_expires or datetime.utcnow() > user.reset_token_expires:
        raise HTTPException(400, "Reset token has expired — please request a new one")

    user.hashed_password     = get_password_hash(body.new_password)
    user.reset_token         = None
    user.reset_token_expires = None
    await user.save()
    return {"msg": "Password reset successfully — you can now sign in"}


# ── Email Verification ────────────────────────────────────────────────────────

@router.post("/send-verification")
async def send_verification(current_user: User = Depends(get_current_user)):
    """
    Generates an email verification token and returns it for EmailJS.
    Rate-limited to 1 request per minute per account.
    """
    if current_user.email_verified:
        return {"msg": "Email is already verified"}

    _check_rate(_verify_rate, current_user.email)

    token = secrets.token_urlsafe(32)
    current_user.verification_token = token
    await current_user.save()

    return {
        "token":     token,
        "email":     current_user.email,
        "full_name": current_user.full_name,
    }


@router.post("/verify-email")
async def verify_email(body: VerifyBody):
    user = await User.find_one(User.verification_token == body.token)
    if not user:
        raise HTTPException(400, "Invalid verification token")
    user.email_verified      = True
    user.verification_token  = None
    await user.save()
    return {"msg": "Email verified successfully! 🎉"}

