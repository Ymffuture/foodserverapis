# routes/webauthn.py
"""
WebAuthn (passkey / fingerprint) authentication for KotaBites.

Flow:
  Registration  (after password login):
    POST /webauthn/register/options  → challenge + pubkey creation params
    POST /webauthn/register/verify   → store credential, return success

  Authentication (instead of password):
    POST /webauthn/auth/options      → challenge + allowed credentials list
    POST /webauthn/auth/verify       → verify assertion, return JWT token

  Management:
    GET    /webauthn/credentials           → list user's passkeys
    PATCH  /webauthn/credentials/{id}      → rename passkey
    DELETE /webauthn/credentials/{id}      → remove passkey
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import webauthn
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from dependencies import create_access_token, get_current_user
from models.user import User
from models.webauthn_credential import WebAuthnCredential

router = APIRouter(prefix="/webauthn", tags=["WebAuthn"])
logger = logging.getLogger(__name__)

# ── Config (set in .env) ───────────────────────────────────────────────────
RP_ID   = os.getenv("WEBAUTHN_RP_ID",   "foodsorder.vercel.app")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "KotaBites")
ORIGIN  = os.getenv("WEBAUTHN_ORIGIN",  "https://foodsorder.vercel.app")
# For local dev add:  WEBAUTHN_RP_ID=localhost  WEBAUTHN_ORIGIN=http://localhost:5173

CHALLENGE_TTL = 120   # seconds

# Simple in-memory challenge store — key → (challenge_bytes, expires_at)
# For multi-instance deployments swap this for Redis.
_challenges: dict[str, tuple[bytes, datetime]] = {}


# ── Challenge helpers ──────────────────────────────────────────────────────

def _save_challenge(key: str, challenge: bytes) -> None:
    _challenges[key] = (challenge, datetime.utcnow() + timedelta(seconds=CHALLENGE_TTL))


def _pop_challenge(key: str) -> bytes:
    entry = _challenges.pop(key, None)
    if not entry:
        raise HTTPException(400, "Challenge not found — please restart the process.")
    challenge, expires = entry
    if datetime.utcnow() > expires:
        raise HTTPException(400, "Challenge expired — please try again.")
    return challenge


# ── Request schemas ────────────────────────────────────────────────────────

class VerifyRegistrationBody(BaseModel):
    credential: dict
    label: Optional[str] = None   # e.g. "My iPhone"


class AuthOptionsBody(BaseModel):
    email: str


class VerifyAuthBody(BaseModel):
    email: str
    credential: dict


class RenameCredentialBody(BaseModel):
    label: str


# ── Registration ───────────────────────────────────────────────────────────

@router.post("/register/options")
async def registration_options(current_user: User = Depends(get_current_user)):
    """
    Generate WebAuthn registration options for the authenticated user.
    Call this after the user has logged in with email/password to enroll their fingerprint.
    """
    existing_creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).to_list()

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(current_user.id).encode(),
        user_name=current_user.email,
        user_display_name=current_user.full_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        timeout=60_000,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in existing_creds
        ],
    )

    _save_challenge(f"reg:{current_user.id}", options.challenge)

    # options_to_json returns a JSON string — parse to dict so FastAPI serialises it
    return json.loads(webauthn.options_to_json(options))


@router.post("/register/verify", status_code=201)
async def verify_registration(
    body: VerifyRegistrationBody,
    current_user: User = Depends(get_current_user),
):
    """Verify the authenticator's attestation and persist the credential."""
    expected_challenge = _pop_challenge(f"reg:{current_user.id}")

    try:
        verification = webauthn.verify_registration_response(
            credential=json.dumps(body.credential),
            expected_challenge=expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning(f"WebAuthn registration failed [{current_user.email}]: {exc}")
        raise HTTPException(400, f"Registration failed: {exc}")

    cred_id_b64 = bytes_to_base64url(verification.credential_id)

    # Guard against duplicate
    if await WebAuthnCredential.find_one(WebAuthnCredential.credential_id == cred_id_b64):
        raise HTTPException(409, "This authenticator is already registered.")

    credential = WebAuthnCredential(
        user_id=str(current_user.id),
        credential_id=cred_id_b64,
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        aaguid=str(verification.aaguid) if verification.aaguid else None,
        device_type=(
            verification.credential_device_type.value
            if verification.credential_device_type else None
        ),
        backed_up=verification.credential_backed_up or False,
        label=body.label or _guess_label(verification.aaguid),
    )
    await credential.insert()

    logger.info(f"Passkey registered | user={current_user.email} | id={cred_id_b64[:12]}…")
    return {
        "verified": True,
        "credential_id": str(credential.id),
        "message": "Fingerprint registered successfully 🔒 — you can now sign in with it.",
    }


# ── Authentication ─────────────────────────────────────────────────────────

@router.post("/auth/options")
async def authentication_options(body: AuthOptionsBody):
    """
    Generate a WebAuthn authentication challenge.
    The email is needed to look up which credentials to allow.
    """
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(404, "No passkey found for this email.")

    stored = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(user.id)
    ).to_list()

    if not stored:
        raise HTTPException(
            404,
            "No fingerprint registered for this account. "
            "Sign in with your password first, then register your fingerprint in settings.",
        )

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in stored
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=60_000,
    )

    _save_challenge(f"auth:{user.id}", options.challenge)
    return json.loads(webauthn.options_to_json(options))


@router.post("/auth/verify")
async def verify_authentication(body: VerifyAuthBody):
    """
    Verify the biometric assertion.
    Returns a JWT access token on success — same shape as /auth/login.
    """
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(401, "Authentication failed.")

    expected_challenge = _pop_challenge(f"auth:{user.id}")

    # Match credential by ID
    cred_id = body.credential.get("id", "")
    stored = await WebAuthnCredential.find_one(
        WebAuthnCredential.credential_id == cred_id,
        WebAuthnCredential.user_id == str(user.id),
    )
    if not stored:
        raise HTTPException(401, "Credential not recognised — please log in with your password.")

    try:
        verification = webauthn.verify_authentication_response(
            credential=json.dumps(body.credential),
            expected_challenge=expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        logger.warning(f"WebAuthn auth failed [{user.email}]: {exc}")
        raise HTTPException(401, f"Authentication failed: {exc}")

    # Update counters
    stored.sign_count = verification.new_sign_count
    stored.last_used_at = datetime.utcnow()
    await stored.save()

    logger.info(f"Passkey login success | user={user.email}")
    return {
        "access_token":   create_access_token({"sub": user.email}),
        "token_type":     "bearer",
        "email":          user.email,
        "full_name":      user.full_name,
        "email_verified": user.email_verified,
        "picture":        user.picture,
    }


# ── Credential management ──────────────────────────────────────────────────

@router.get("/credentials")
async def list_credentials(current_user: User = Depends(get_current_user)):
    """List all registered passkeys for the authenticated user."""
    creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).sort("-created_at").to_list()

    return [
        {
            "id":           str(c.id),
            "label":        c.label or "Passkey",
            "device_type":  c.device_type,
            "backed_up":    c.backed_up,
            "aaguid":       c.aaguid,
            "created_at":   c.created_at,
            "last_used_at": c.last_used_at,
        }
        for c in creds
    ]


@router.patch("/credentials/{doc_id}")
async def rename_credential(
    doc_id: str,
    body: RenameCredentialBody,
    current_user: User = Depends(get_current_user),
):
    """Rename a passkey (e.g. 'Work MacBook')."""
    cred = await WebAuthnCredential.get(doc_id)
    if not cred or cred.user_id != str(current_user.id):
        raise HTTPException(404, "Passkey not found.")
    cred.label = body.label.strip()[:60]
    await cred.save()
    return {"success": True, "label": cred.label}


@router.delete("/credentials/{doc_id}")
async def delete_credential(
    doc_id: str,
    current_user: User = Depends(get_current_user),
):
    """Remove a registered passkey."""
    cred = await WebAuthnCredential.get(doc_id)
    if not cred or cred.user_id != str(current_user.id):
        raise HTTPException(404, "Passkey not found.")
    await cred.delete()
    logger.info(f"Passkey removed | user={current_user.email} | id={doc_id}")
    return {"success": True, "message": "Passkey removed."}


# ── Helpers ────────────────────────────────────────────────────────────────

_AAGUID_LABELS: dict[str, str] = {
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Windows",
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello PIN",
    "f8a011f3-8c0a-4d15-8006-17111f9edc7d": "Security Key",
    "b93fd961-f2e6-462f-b122-82002247de78": "Android Fingerprint",
    "00000000-0000-0000-0000-000000000000": "Passkey",
}


def _guess_label(aaguid) -> str:
    if aaguid is None:
        return "Passkey"
    return _AAGUID_LABELS.get(str(aaguid), "Passkey")
