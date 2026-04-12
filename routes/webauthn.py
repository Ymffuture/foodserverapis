# routes/webauthn.py
"""
WebAuthn (Passkey / Fingerprint) Authentication for KotaBites
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
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

# ── Config ────────────────────────────────────────────────────────────────
RP_ID = os.getenv("WEBAUTHN_RP_ID", "foodsorder.vercel.app")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "KotaBites")
ORIGIN = os.getenv("WEBAUTHN_ORIGIN", "https://foodsorder.vercel.app")

CHALLENGE_TTL = 120  # seconds

# In-memory challenge store (for production → use Redis)
_challenges: dict[str, tuple[bytes, datetime]] = {}


# ── Challenge Helpers ─────────────────────────────────────────────────────
def _save_challenge(key: str, challenge: bytes) -> None:
    _challenges[key] = (challenge, datetime.utcnow() + timedelta(seconds=CHALLENGE_TTL))


def _pop_challenge(key: str) -> bytes:
    entry = _challenges.pop(key, None)
    if not entry:
        raise HTTPException(status_code=400, detail="Challenge not found or expired. Please start over.")

    challenge, expires = entry
    if datetime.utcnow() > expires:
        raise HTTPException(status_code=400, detail="Challenge expired. Please try again.")

    return challenge


# ── Schemas ───────────────────────────────────────────────────────────────
class VerifyRegistrationBody(BaseModel):
    credential: dict
    label: Optional[str] = None


class AuthOptionsBody(BaseModel):
    email: str


class VerifyAuthBody(BaseModel):
    email: str
    credential: dict


class RenameCredentialBody(BaseModel):
    label: str


# ── Registration ──────────────────────────────────────────────────────────
@router.post("/register/options")
async def registration_options(current_user: User = Depends(get_current_user)):
    """Generate registration options for the logged-in user."""
    # Get existing credentials to exclude
    existing_creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).to_list()

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(current_user.id).encode("utf-8"),
        user_name=current_user.email,
        user_display_name=current_user.full_name or current_user.email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        timeout=60000,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in existing_creds
        ],
    )

    _save_challenge(f"reg:{current_user.id}", options.challenge)

    return json.loads(webauthn.options_to_json(options))


@router.post("/register/verify", status_code=201)
async def verify_registration(
    body: VerifyRegistrationBody,
    current_user: User = Depends(get_current_user),
):
    """Verify registration and save credential."""
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
        logger.warning(f"WebAuthn registration verification failed for {current_user.email}: {exc}")
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(exc)}")

    cred_id_b64 = bytes_to_base64url(verification.credential_id)

    # Prevent duplicate registration
    if await WebAuthnCredential.find_one(WebAuthnCredential.credential_id == cred_id_b64):
        raise HTTPException(status_code=409, detail="This passkey is already registered.")

    credential = WebAuthnCredential(
        user_id=str(current_user.id),
        credential_id=cred_id_b64,
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        aaguid=str(verification.aaguid) if verification.aaguid else None,
        device_type=verification.credential_device_type.value if verification.credential_device_type else None,
        backed_up=verification.credential_backed_up or False,
        label=body.label or _guess_label(verification.aaguid),
        created_at=datetime.utcnow(),
    )

    await credential.insert()

    logger.info(f"Passkey registered successfully for {current_user.email}")
    return {
        "verified": True,
        "message": "Fingerprint / Passkey registered successfully! You can now sign in with it.",
    }


# ── Authentication ────────────────────────────────────────────────────────
@router.post("/auth/options")
async def authentication_options(body: AuthOptionsBody):
    """Generate authentication challenge for a given email."""
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(status_code=404, detail="No account found with this email.")

    stored_creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(user.id)
    ).to_list()

    if not stored_creds:
        raise HTTPException(
            status_code=404,
            detail="No passkey registered for this account. Please sign in with password first."
        )

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in stored_creds
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=60000,
    )

    _save_challenge(f"auth:{user.id}", options.challenge)

    return json.loads(webauthn.options_to_json(options))


@router.post("/auth/verify")
async def verify_authentication(body: VerifyAuthBody):
    """Verify passkey assertion and return JWT token."""
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(status_code=401, detail="Authentication failed.")

    expected_challenge = _pop_challenge(f"auth:{user.id}")

    cred_id = body.credential.get("id", "")
    stored = await WebAuthnCredential.find_one(
        WebAuthnCredential.credential_id == cred_id,
        WebAuthnCredential.user_id == str(user.id),
    )
    if not stored:
        raise HTTPException(status_code=401, detail="Passkey not recognized.")

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
        logger.warning(f"WebAuthn authentication failed for {user.email}: {exc}")
        raise HTTPException(status_code=401, detail="Authentication failed.")

    # Update sign count and last used
    stored.sign_count = verification.new_sign_count
    stored.last_used_at = datetime.utcnow()
    await stored.save()

    logger.info(f"Passkey login successful for {user.email}")

    return {
        "access_token": create_access_token({"sub": user.email}),
        "token_type": "bearer",
        "email": user.email,
        "full_name": user.full_name,
    }


# ── Credential Management ──────────────────────────────────────────────────
@router.get("/credentials")
async def list_credentials(current_user: User = Depends(get_current_user)):
    creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).sort("-created_at").to_list()

    return [
        {
            "id": str(c.id),
            "label": c.label or "Passkey",
            "device_type": c.device_type,
            "backed_up": c.backed_up,
            "created_at": c.created_at,
            "last_used_at": c.last_used_at,
        }
        for c in creds
    ]


@router.patch("/credentials/{cred_id}")
async def rename_credential(
    cred_id: str,
    body: RenameCredentialBody,
    current_user: User = Depends(get_current_user),
):
    cred = await WebAuthnCredential.get(cred_id)
    if not cred or cred.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Passkey not found.")

    cred.label = body.label.strip()[:60]
    await cred.save()
    return {"success": True, "label": cred.label}


@router.delete("/credentials/{cred_id}")
async def delete_credential(
    cred_id: str,
    current_user: User = Depends(get_current_user),
):
    cred = await WebAuthnCredential.get(cred_id)
    if not cred or cred.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Passkey not found.")

    await cred.delete()
    logger.info(f"Passkey deleted by {current_user.email}")
    return {"success": True, "message": "Passkey removed successfully."}


# ── Helper ────────────────────────────────────────────────────────────────
def _guess_label(aaguid: Optional[str]) -> str:
    labels = {
        "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Windows",
        "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
        "b93fd961-f2e6-462f-b122-82002247de78": "Android Fingerprint",
        "f8a011f3-8c0a-4d15-8006-17111f9edc7d": "Security Key",
    }
    return labels.get(str(aaguid), "Passkey") if aaguid else "Passkey"
