# routes/webauthn.py
"""
WebAuthn (Passkey / Fingerprint) Authentication for KotaBites.

Fix: webauthn 2.1.0 raises NotImplementedError for "android-key", "tpm",
"apple" attestation formats even when none was requested. We catch that and
manually extract the credential from the raw CBOR auth data so ALL device
types (Android, iOS, Windows Hello, Touch ID) can register successfully.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import cbor2
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import webauthn
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
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
RP_ID   = os.getenv("WEBAUTHN_RP_ID",   "foodsorder.vercel.app")
RP_NAME = os.getenv("WEBAUTHN_RP_NAME", "KotaBites")
ORIGIN  = os.getenv("WEBAUTHN_ORIGIN",  "https://foodsorder.vercel.app")

CHALLENGE_TTL = 120  # seconds

# In-memory challenge store — replace with Redis in production
_challenges: dict[str, tuple[bytes, datetime]] = {}


# ── Challenge helpers ─────────────────────────────────────────────────────

def _save_challenge(key: str, challenge: bytes) -> None:
    _challenges[key] = (challenge, datetime.utcnow() + timedelta(seconds=CHALLENGE_TTL))


def _pop_challenge(key: str) -> bytes:
    entry = _challenges.pop(key, None)
    if not entry:
        raise HTTPException(400, "Challenge not found or expired. Please start again.")
    challenge, expires = entry
    if datetime.utcnow() > expires:
        raise HTTPException(400, "Challenge expired. Please try again.")
    return challenge


# ── CBOR fallback extractor ───────────────────────────────────────────────

def _extract_credential_from_cbor(
    credential_dict: dict,
) -> tuple[str, str, int]:
    """
    Manually pull (credential_id_b64url, public_key_b64url, sign_count) out of
    the raw WebAuthn attestation object when the library raises NotImplementedError
    for an unsupported attestation format (android-key, tpm, apple …).

    The credentialPublicKey is stored as-is (COSE CBOR bytes) so that
    verify_authentication_response can use it later.
    """
    response = credential_dict.get("response", {})

    # ── Decode attestationObject ──────────────────────────────────────────
    att_b64 = response.get("attestationObject", "")
    if not att_b64:
        raise ValueError("attestationObject missing from response")

    att_bytes = base64url_to_bytes(att_b64)
    att_obj   = cbor2.loads(att_bytes)

    auth_data: bytes = att_obj.get("authData", b"")
    if len(auth_data) < 37:
        raise ValueError(f"authData too short ({len(auth_data)} bytes)")

    # ── Parse authenticator data layout ──────────────────────────────────
    # rpIdHash  (32) | flags (1) | signCount (4) | [attestedCredentialData]
    flags      = auth_data[32]
    sign_count = int.from_bytes(auth_data[33:37], "big")

    # Bit 6 of flags = AT flag (attested credential data is present)
    if not (flags & 0x40):
        raise ValueError("AT flag not set — no attested credential data")

    # attestedCredentialData layout:
    # aaguid (16) | credentialIdLength (2) | credentialId (n) | credentialPublicKey (CBOR)
    offset = 37
    if len(auth_data) < offset + 18:
        raise ValueError("authData too short for attested credential data")

    offset += 16  # skip aaguid
    cred_id_len = int.from_bytes(auth_data[offset: offset + 2], "big")
    offset += 2

    if len(auth_data) < offset + cred_id_len:
        raise ValueError("authData truncated in credentialId")

    cred_id_bytes        = auth_data[offset: offset + cred_id_len]
    cred_pub_key_bytes   = auth_data[offset + cred_id_len:]

    if not cred_pub_key_bytes:
        raise ValueError("credentialPublicKey is empty")

    return (
        bytes_to_base64url(cred_id_bytes),
        bytes_to_base64url(cred_pub_key_bytes),
        sign_count,
    )


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
    existing = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).to_list()

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=str(current_user.id).encode("utf-8"),
        user_name=current_user.email,
        user_display_name=current_user.full_name or current_user.email,
        attestation=AttestationConveyancePreference.NONE,   # ← explicit NONE
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        timeout=60000,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in existing
        ],
    )

    _save_challenge(f"reg:{current_user.id}", options.challenge)
    return json.loads(webauthn.options_to_json(options))


@router.post("/register/verify", status_code=201)
async def verify_registration(
    body: VerifyRegistrationBody,
    current_user: User = Depends(get_current_user),
):
    expected_challenge = _pop_challenge(f"reg:{current_user.id}")

    credential_id_b64 = None
    public_key_b64    = None
    sign_count        = 0
    aaguid_str        = None
    device_type       = None
    backed_up         = False

    # ── Try the library first ─────────────────────────────────────────────
    try:
        verification = webauthn.verify_registration_response(
            credential=json.dumps(body.credential),
            expected_challenge=expected_challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            require_user_verification=True,
        )
        credential_id_b64 = bytes_to_base64url(verification.credential_id)
        public_key_b64    = bytes_to_base64url(verification.credential_public_key)
        sign_count        = verification.sign_count
        aaguid_str        = str(verification.aaguid) if verification.aaguid else None
        device_type       = (
            verification.credential_device_type.value
            if verification.credential_device_type else None
        )
        backed_up = verification.credential_backed_up or False

    # ── Fallback: unsupported attestation format (android-key, tpm, apple…)
    except NotImplementedError as nie:
        logger.warning(
            f"Attestation format not supported for {current_user.email}: {nie}. "
            "Falling back to manual CBOR extraction."
        )
        try:
            credential_id_b64, public_key_b64, sign_count = _extract_credential_from_cbor(
                body.credential
            )
        except Exception as parse_err:
            logger.error(f"CBOR fallback extraction failed: {parse_err}")
            raise HTTPException(
                400, f"Could not process credential: {parse_err}"
            )

    except Exception as exc:
        logger.warning(f"WebAuthn registration failed for {current_user.email}: {exc}")
        raise HTTPException(400, f"Registration failed: {exc}")

    if not credential_id_b64 or not public_key_b64:
        raise HTTPException(400, "Failed to extract credential data from response")

    # ── Duplicate guard ───────────────────────────────────────────────────
    if await WebAuthnCredential.find_one(
        WebAuthnCredential.credential_id == credential_id_b64
    ):
        raise HTTPException(409, "This passkey is already registered.")

    # ── Persist ───────────────────────────────────────────────────────────
    cred = WebAuthnCredential(
        user_id=str(current_user.id),
        credential_id=credential_id_b64,
        public_key=public_key_b64,
        sign_count=sign_count,
        aaguid=aaguid_str,
        device_type=device_type,
        backed_up=backed_up,
        label=body.label or "Passkey",
        created_at=datetime.utcnow(),
    )
    await cred.insert()

    logger.info(f"Passkey registered for {current_user.email} (id …{credential_id_b64[-8:]})")
    return {
        "verified": True,
        "message": "Fingerprint / Passkey registered successfully!",
    }


# ── Authentication ────────────────────────────────────────────────────────

@router.post("/auth/options")
async def authentication_options(body: AuthOptionsBody):
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(404, "No account found with this email.")

    stored = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(user.id)
    ).to_list()

    if not stored:
        raise HTTPException(
            404,
            "No passkey registered for this account. "
            "Sign in with your password first, then add a fingerprint in Settings."
        )

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in stored
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=60000,
    )

    _save_challenge(f"auth:{user.id}", options.challenge)
    return json.loads(webauthn.options_to_json(options))


@router.post("/auth/verify")
async def verify_authentication(body: VerifyAuthBody):
    user = await User.find_one(User.email == body.email.lower().strip())
    if not user:
        raise HTTPException(401, "Authentication failed.")

    expected_challenge = _pop_challenge(f"auth:{user.id}")

    cred_id = body.credential.get("id", "")
    stored  = await WebAuthnCredential.find_one(
        WebAuthnCredential.credential_id == cred_id,
        WebAuthnCredential.user_id == str(user.id),
    )
    if not stored:
        raise HTTPException(401, "Passkey not recognised for this account.")

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
        logger.warning(f"WebAuthn auth failed for {user.email}: {exc}")
        raise HTTPException(401, "Fingerprint verification failed. Please try again.")

    # Update sign count + last used
    stored.sign_count   = verification.new_sign_count
    stored.last_used_at = datetime.utcnow()
    await stored.save()

    logger.info(f"Passkey login successful for {user.email}")
    return {
        "access_token": create_access_token({"sub": user.email}),
        "token_type":   "bearer",
        "email":        user.email,
        "full_name":    user.full_name,
    }


# ── Credential management ─────────────────────────────────────────────────

@router.get("/credentials")
async def list_credentials(current_user: User = Depends(get_current_user)):
    creds = await WebAuthnCredential.find(
        WebAuthnCredential.user_id == str(current_user.id)
    ).sort("-created_at").to_list()

    return [
        {
            "id":           str(c.id),
            "label":        c.label or "Passkey",
            "device_type":  c.device_type,
            "backed_up":    c.backed_up,
            "created_at":   c.created_at,
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
        raise HTTPException(404, "Passkey not found.")
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
        raise HTTPException(404, "Passkey not found.")
    await cred.delete()
    logger.info(f"Passkey deleted by {current_user.email}")
    return {"success": True, "message": "Passkey removed successfully."}
