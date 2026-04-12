# models/webauthn_credential.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import Optional


class WebAuthnCredential(Document):
    """Stores a user's registered passkey / fingerprint credential."""
    user_id: str
    credential_id: str           # base64url-encoded credential ID from authenticator
    public_key: str              # base64url-encoded COSE public key
    sign_count: int = 0          # monotonic counter — detects credential cloning
    aaguid: Optional[str] = None # authenticator model identifier (e.g. TouchID, Windows Hello)
    device_type: Optional[str] = None  # "singleDevice" | "multiDevice"
    backed_up: bool = False      # whether credential is synced to iCloud/Google etc.
    label: Optional[str] = None  # user-friendly name e.g. "iPhone Touch ID"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None

    class Settings:
        name = "webauthn_credentials"
        indexes = [
            "user_id",
            "credential_id",  # unique index set in migration
            [("user_id", 1), ("created_at", -1)],
        ]
