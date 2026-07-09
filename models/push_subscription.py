# models/push_subscription.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import Optional


class PushSubscription(Document):
    """
    One browser Push API subscription (one per browser/device the user has
    granted notification permission on — a single user can have several).

    `endpoint` is the natural unique key: the browser's push service assigns
    it, and re-subscribing on the same device/browser returns the same
    endpoint, so subscribing is an upsert keyed on it rather than a plain
    insert (see routes/push.py).
    """
    user_id:  str
    endpoint: str

    # From PushSubscriptionKeys — needed to encrypt the payload the browser
    # can decrypt (see services/push_service.py).
    p256dh: str
    auth:   str

    user_agent: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None

    class Settings:
        name = "push_subscriptions"
        indexes = [
            "user_id",
            "endpoint",
            [("user_id", 1), ("endpoint", 1)],
        ]
