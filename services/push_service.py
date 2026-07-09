# services/push_service.py
"""
Sends real browser push notifications (Push API, not the in-app bell) via
the Web Push protocol + VAPID auth, using pywebpush.

This is deliberately separate from routes/notifications.py's AppNotification
system: that's an in-app inbox the user only sees while the tab is open (or
next time they open it). This module is what makes a notification actually
pop up on the user's device even when KotaBites isn't open at all — the
thing the service worker's `push` event listener (public/sw.js) renders.

Regenerating your own VAPID keypair for production (one-off, not needed for
local dev — config.py ships a working default pair):

    from cryptography.hazmat.primitives.asymmetric import ec
    import base64

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_numbers()
    raw_public  = b"\\x04" + pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
    raw_private = key.private_numbers().private_value.to_bytes(32, "big")
    print("VAPID_PUBLIC_KEY =", b64url(raw_public))
    print("VAPID_PRIVATE_KEY =", b64url(raw_private))
"""
import json
import logging
import asyncio
from typing import Optional

from pywebpush import webpush, WebPushException

from config import VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIM_EMAIL
from models.push_subscription import PushSubscription

logger = logging.getLogger(__name__)


def _send_sync(sub: PushSubscription, payload: dict) -> Optional[int]:
    """
    Blocking pywebpush call — run inside a thread (see `_send_one`) since
    the rest of this codebase is async and this isn't.

    Returns the HTTP status the push service responded with, or None if the
    subscription is dead (404/410 — the browser unsubscribed or the token
    expired) so the caller knows to delete it. Any other failure re-raises.
    """
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIM_EMAIL},
        )
        return 201
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            return None  # dead subscription — caller should delete it
        logger.warning(f"Push send failed for {sub.endpoint[:60]}…: {exc}")
        raise


async def _send_and_cleanup(sub: PushSubscription, payload: dict):
    try:
        result = await asyncio.to_thread(_send_sync, sub, payload)
        if result is None:
            # Dead subscription (unsubscribed / expired) — remove it so we
            # stop wasting a push call on it every time.
            await sub.delete()
    except Exception:
        pass  # already logged in _send_sync; don't let one bad send break the batch


def _build_payload(title: str, message: str, url: str = "/") -> dict:
    return {
        "title": title,
        "body":  message,
        "url":   url,
        "icon":  "/logo-192.png",
        "badge": "/logo-192.png",
    }


async def send_push_to_user(user_id: str, title: str, message: str, url: str = "/") -> int:
    """Push to every device a specific user has subscribed on. Returns subscription count attempted."""
    subs = await PushSubscription.find(PushSubscription.user_id == user_id).to_list()
    if not subs:
        return 0
    payload = _build_payload(title, message, url)
    await asyncio.gather(*(_send_and_cleanup(s, payload) for s in subs))
    return len(subs)


async def send_push_to_all(title: str, message: str, url: str = "/") -> int:
    """Broadcast to every subscribed device across every user. Returns subscription count attempted."""
    subs = await PushSubscription.find_all().to_list()
    if not subs:
        return 0
    payload = _build_payload(title, message, url)
    # Batch in chunks so one giant broadcast doesn't open thousands of
    # threads at once.
    CHUNK = 200
    for i in range(0, len(subs), CHUNK):
        chunk = subs[i:i + CHUNK]
        await asyncio.gather(*(_send_and_cleanup(s, payload) for s in chunk))
    return len(subs)
