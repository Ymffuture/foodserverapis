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
from config import (
    GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET,
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
)

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

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    email: str
    full_name: str
    email_verified: bool
    picture: Optional[str] = None

class GoogleBody(BaseModel):
    access_token: str

class GitHubBody(BaseModel):
    code: str
    redirect_uri: str

class SpotifyBody(BaseModel):
    code: str
    redirect_uri: str

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


def _oauth_response(user: User) -> dict:
    """Shared response shape for all OAuth providers."""
    return {
        "access_token": create_access_token({"sub": user.email}),
        "token_type":   "bearer",
        "user": {
            "email":          user.email,
            "full_name":      user.full_name,
            "picture":        user.picture or "",
            "email_verified": user.email_verified,
        },
    }


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


@router.post("/login", response_model=LoginResponse)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await User.find_one(User.email == form_data.username)

    if not user or not user.hashed_password or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"})

    if not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail="Please verify your email before logging in. Check your inbox for the verification link."
        )

    return {
        "access_token": create_access_token({"sub": user.email}),
        "token_type":   "bearer",
        "email":        user.email,
        "full_name":    user.full_name,
        "email_verified": user.email_verified,
        "picture":      user.picture,
    }


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

    return _oauth_response(user)


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@router.post("/github")
async def github_login(body: GitHubBody):
    """
    Exchange a GitHub OAuth authorization code for a user account.

    Frontend flow:
      1. Redirect user to:
         https://github.com/login/oauth/authorize
           ?client_id=<GITHUB_CLIENT_ID>
           &redirect_uri=<your_redirect_uri>
           &scope=read:user user:email
      2. GitHub redirects back with ?code=...
      3. Frontend POSTs { code, redirect_uri } here.
    """
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        raise HTTPException(503, "GitHub OAuth is not configured on the server")

    async with httpx.AsyncClient() as http:
        # Step 1: exchange code for access token
        token_resp = await http.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id":     GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code":          body.code,
                "redirect_uri":  body.redirect_uri,
            },
            headers={"Accept": "application/json"},
        )

    if token_resp.status_code != 200:
        raise HTTPException(401, "GitHub token exchange failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        error = token_data.get("error_description", "No access token returned")
        raise HTTPException(401, f"GitHub OAuth error: {error}")

    async with httpx.AsyncClient() as http:
        # Step 2: fetch user profile
        profile_resp = await http.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept":        "application/vnd.github+json",
            },
        )
        # Step 3: fetch verified emails (profile email may be null if user hid it)
        emails_resp = await http.get(
            "https://api.github.com/user/emails",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept":        "application/vnd.github+json",
            },
        )

    if profile_resp.status_code != 200:
        raise HTTPException(401, "Could not fetch GitHub profile")

    profile = profile_resp.json()
    github_id = str(profile.get("id", ""))

    # Resolve the best available email
    email = profile.get("email")
    if not email and emails_resp.status_code == 200:
        emails = emails_resp.json()
        # Prefer primary + verified, then any verified, then any
        primary = next((e["email"] for e in emails if e.get("primary") and e.get("verified")), None)
        verified = next((e["email"] for e in emails if e.get("verified")), None)
        any_email = next((e["email"] for e in emails), None)
        email = primary or verified or any_email

    if not email:
        raise HTTPException(400, "Your GitHub account has no accessible email address. "
                                 "Please make your email public on GitHub, or use a different login method.")

    avatar = profile.get("avatar_url")
    display_name = profile.get("name") or profile.get("login") or email.split("@")[0]

    # Upsert user — link to existing account by email if present
    user = await User.find_one(User.email == email)
    if not user:
        user = User(
            email=email,
            hashed_password=None,
            full_name=display_name,
            phone=None,
            github_id=github_id,
            picture=avatar,
            email_verified=True,   # GitHub emails are verified by GitHub
        )
        await user.insert()
        logger.info(f"New GitHub user created: {email} (gh:{github_id})")
    else:
        changed = False
        if not user.github_id:
            user.github_id = github_id; changed = True
        if avatar and user.picture != avatar and not user.picture:
            user.picture = avatar; changed = True
        if not user.email_verified:
            user.email_verified = True; changed = True
        if changed:
            await user.save()

    return _oauth_response(user)


# ── Spotify OAuth ─────────────────────────────────────────────────────────────

@router.post("/spotify")
async def spotify_login(body: SpotifyBody):
    """
    Exchange a Spotify OAuth authorization code for a user account.

    Frontend flow:
      1. Redirect user to:
         https://accounts.spotify.com/authorize
           ?client_id=<SPOTIFY_CLIENT_ID>
           &response_type=code
           &redirect_uri=<your_redirect_uri>
           &scope=user-read-email user-read-private
      2. Spotify redirects back with ?code=...
      3. Frontend POSTs { code, redirect_uri } here.

    NOTE: Spotify accounts may not have an email if the user signed up
    via Facebook without sharing their email. In that case we return a
    clear 400 error.
    """
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise HTTPException(503, "Spotify OAuth is not configured on the server")

    import base64
    credentials = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient() as http:
        # Step 1: exchange code for access token
        token_resp = await http.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type":   "authorization_code",
                "code":         body.code,
                "redirect_uri": body.redirect_uri,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
        )

    if token_resp.status_code != 200:
        detail = token_resp.json().get("error_description", "Spotify token exchange failed")
        raise HTTPException(401, detail)

    token_data  = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(401, "No access token returned from Spotify")

    async with httpx.AsyncClient() as http:
        # Step 2: fetch user profile
        profile_resp = await http.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if profile_resp.status_code != 200:
        raise HTTPException(401, "Could not fetch Spotify profile")

    profile    = profile_resp.json()
    spotify_id = profile.get("id", "")
    email      = profile.get("email")

    if not email:
        raise HTTPException(
            400,
            "Your Spotify account has no email address associated with it. "
            "Please use a different login method or add an email to your Spotify account."
        )

    display_name = profile.get("display_name") or email.split("@")[0]
    # Spotify returns images as a list of dicts; grab the first one if present
    images = profile.get("images") or []
    avatar = images[0].get("url") if images else None

    user = await User.find_one(User.email == email)
    if not user:
        user = User(
            email=email,
            hashed_password=None,
            full_name=display_name,
            phone=None,
            spotify_id=spotify_id,
            picture=avatar,
            email_verified=True,   # Spotify emails are verified by Spotify
        )
        await user.insert()
        logger.info(f"New Spotify user created: {email} (sp:{spotify_id})")
    else:
        changed = False
        if not user.spotify_id:
            user.spotify_id = spotify_id; changed = True
        if avatar and not user.picture:
            user.picture = avatar; changed = True
        if not user.email_verified:
            user.email_verified = True; changed = True
        if changed:
            await user.save()

    return _oauth_response(user)


# ── Password Reset ────────────────────────────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(body: ForgotBody):
    _check_rate(_reset_rate, body.email)

    user = await User.find_one(User.email == body.email)
    if not user:
        return {"msg": "If that email is registered, a reset link will be sent."}
    if not user.hashed_password:
        raise HTTPException(400, "This account uses social sign-in — no password to reset.")

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
