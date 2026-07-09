# routes/push.py
"""
Web Push subscription management.

  GET  /push/vapid-public-key   – frontend fetches this for pushManager.subscribe()
  POST /push/subscribe          – save/refresh a browser's push subscription
  POST /push/unsubscribe        – remove one (e.g. user disables notifications)
  POST /push/test               – send yourself a test push (handy for debugging)
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_user
from models.user import User
from models.push_subscription import PushSubscription
from config import VAPID_PUBLIC_KEY
from services.push_service import send_push_to_user

router = APIRouter(prefix="/push", tags=["Push Notifications"])
logger = logging.getLogger(__name__)


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth:   str


class SubscribeRequest(BaseModel):
    endpoint:    str
    keys:        SubscriptionKeys
    user_agent:  Optional[str] = Field(None, max_length=300)


class UnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/vapid-public-key")
async def get_vapid_public_key():
    """Public — no auth needed, this key is meant to be shipped to the client."""
    return {"public_key": VAPID_PUBLIC_KEY}


@router.post("/subscribe", status_code=201)
async def subscribe(body: SubscribeRequest, current_user: User = Depends(get_current_user)):
    uid = str(current_user.id)

    # Upsert on endpoint — re-subscribing on the same browser/device returns
    # the same endpoint from the Push API, and a user can legitimately have
    # multiple endpoints (phone + laptop, several browsers, etc).
    existing = await PushSubscription.find_one(PushSubscription.endpoint == body.endpoint)
    if existing:
        existing.user_id      = uid  # ownership can change (shared device, re-login as someone else)
        existing.p256dh       = body.keys.p256dh
        existing.auth         = body.keys.auth
        existing.user_agent   = body.user_agent
        existing.last_used_at = datetime.utcnow()
        await existing.save()
        return {"success": True, "message": "Subscription refreshed"}

    sub = PushSubscription(
        user_id=uid,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=body.user_agent,
    )
    await sub.insert()
    logger.info(f"Push subscription created for {current_user.email}")
    return {"success": True, "message": "Subscribed to push notifications"}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeRequest, current_user: User = Depends(get_current_user)):
    sub = await PushSubscription.find_one(
        PushSubscription.endpoint == body.endpoint,
        PushSubscription.user_id == str(current_user.id),
    )
    if sub:
        await sub.delete()
    return {"success": True, "message": "Unsubscribed"}


@router.post("/test")
async def send_test_push(current_user: User = Depends(get_current_user)):
    """Send the calling user a test push on all their subscribed devices — for debugging the setup."""
    count = await send_push_to_user(
        str(current_user.id),
        title="🍕 Test notification",
        message="Push notifications are working!",
        url="/",
    )
    if count == 0:
        raise HTTPException(404, "No push subscriptions found for your account — enable notifications first.")
    return {"success": True, "sent_to_devices": count}
