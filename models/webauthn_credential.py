from beanie import Document
from datetime import datetime
from typing import Optional

class WebAuthnCredential(Document):
    user_id: str
    credential_id: str
    public_key: str
    sign_count: int
    aaguid: Optional[str] = None
    device_type: Optional[str] = None
    backed_up: bool = False
    label: Optional[str] = None
    created_at: datetime = datetime.utcnow()
    last_used_at: Optional[datetime] = None

    class Settings:
        name = "webauthn_credentials"
