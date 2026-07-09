# routes/notifications.py
"""
Notification system — admin broadcasts or targets a single user.

Admin:
  POST   /notifications                   – create notification
  GET    /notifications/admin/all         – list all (admin view with read counts)
  PATCH  /notifications/{id}/deactivate
  DELETE /notifications/{id}
  GET    /notifications/debug             – raw collection dump (admin, dev use)

Users (customer-facing app):
  GET    /notifications/my                – notifications for me (broadcast + targeted)
  GET    /notifications/my/unread-count  – quick badge count
  PATCH  /notifications/{id}/read        – mark one as read
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_admin_user, get_current_user
from models.user import User
from models.notification import AppNotification, NotificationType, NotificationTarget
from services.push_service import send_push_to_user, send_push_to_all

router = APIRouter(prefix="/notifications", tags=["Notifications"])
logger = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────────

class CreateNotificationRequest(BaseModel):
    title:          str              = Field(..., min_length=3, max_length=100)
    message:        str              = Field(..., min_length=5, max_length=2000)
    type:           NotificationType = NotificationType.INFO
    target:         NotificationTarget = NotificationTarget.ALL
    target_user_id: Optional[str]   = None   # required if target == specific
    expires_days:   int              = Field(default=30, ge=1, le=365)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize(n: AppNotification, viewer_id: Optional[str] = None) -> dict:
    read_by: list = n.read_by or []
    return {
        "id":              str(n.id),
        "title":           n.title,
        "message":         n.message,
        "type":            n.type.value if hasattr(n.type, "value") else str(n.type),
        "target":          n.target.value if hasattr(n.target, "value") else str(n.target),
        "target_user_id":  n.target_user_id,
        "created_by":      n.created_by,
        "created_by_name": n.created_by_name,
        "is_active":       n.is_active,
        "read_by_count":   len(read_by),
        "is_read":         (viewer_id in read_by) if viewer_id else False,
        "created_at":      n.created_at,
        "expires_at":      n.expires_at,
    }


def _matches_user(n: AppNotification, uid: str) -> bool:
    """
    Return True if this notification should be shown to this user.
    Avoids $or in Mongo — do it in Python instead.
    """
    target_val = n.target.value if hasattr(n.target, "value") else str(n.target)
    if target_val == NotificationTarget.ALL.value:
        return True
    if target_val == NotificationTarget.SPECIFIC.value and n.target_user_id == uid:
        return True
    return False


async def _fetch_active_for_user(uid: str) -> list[AppNotification]:
    """
    Fetch ALL active, non-expired notifications and filter for this user in Python.
    This avoids raw $or dict queries that can silently misbehave in Beanie v1.28.
    """
    now = datetime.utcnow()

    # Use Beanie native operators — no raw dict, no $or
    all_active = await AppNotification.find(
        AppNotification.is_active == True,
        AppNotification.expires_at > now,
    ).sort(
        [("created_at", -1)]   # motor-style sort tuple — works reliably in Beanie v1.28
    ).limit(200).to_list()

    return [n for n in all_active if _matches_user(n, uid)]


# ── Admin: create ─────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def create_notification(
    body:  CreateNotificationRequest,
    admin: User = Depends(get_current_admin_user),
):
    if body.target == NotificationTarget.SPECIFIC and not body.target_user_id:
        raise HTTPException(422, "target_user_id is required when target is 'specific'")

    if body.target_user_id:
        target_user = await User.get(body.target_user_id)
        if not target_user:
            raise HTTPException(404, "Target user not found")

    expires = datetime.utcnow() + timedelta(days=body.expires_days)

    # Guard: full_name may be None on legacy accounts
    sender_name = admin.full_name or admin.email or "Admin"

    notif = AppNotification(
        title=body.title,
        message=body.message,
        type=body.type,
        target=body.target,
        target_user_id=body.target_user_id,
        created_by=str(admin.id),
        created_by_name=sender_name,
        is_active=True,
        read_by=[],
        expires_at=expires,
    )

    try:
        await notif.insert()
    except Exception as exc:
        logger.exception(f"Notification insert failed: {exc}")
        raise HTTPException(500, f"Failed to save notification: {exc}")

    logger.info(
        f"Notification '{body.title}' created by {admin.email} "
        f"→ {body.target.value}"
        + (f" → user {body.target_user_id}" if body.target_user_id else "")
    )

    # Also fire a real browser push (Push API) so subscribed users get a
    # native OS notification even when KotaBites isn't open in a tab —
    # the in-app inbox above only shows up while/when they're in the app.
    # Best-effort: a push failure should never fail the notification create.
    try:
        if body.target == NotificationTarget.SPECIFIC and body.target_user_id:
            await send_push_to_user(body.target_user_id, body.title, body.message, url="/")
        else:
            await send_push_to_all(body.title, body.message, url="/")
    except Exception as exc:
        logger.warning(f"Push dispatch failed for notification '{body.title}': {exc}")

    return {"success": True, "message": "Notification sent", "notification": _serialize(notif)}


# ── Admin: list all ──────────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_notifications(
    limit:       int  = 100,
    active_only: bool = False,
    admin: User = Depends(get_current_admin_user),
):
    try:
        if active_only:
            notifications = await AppNotification.find(
                AppNotification.is_active == True
            ).sort([("created_at", -1)]).limit(limit).to_list()
        else:
            notifications = await AppNotification.find_all().sort(
                [("created_at", -1)]
            ).limit(limit).to_list()
    except Exception as exc:
        logger.exception(f"admin_list_notifications failed: {exc}")
        raise HTTPException(500, "Failed to fetch notifications")

    return [_serialize(n) for n in notifications]


# ── Admin: debug dump ─────────────────────────────────────────────────────────

@router.get("/debug")
async def debug_notifications(
    admin: User = Depends(get_current_admin_user),
):
    """
    Raw collection health-check. Returns counts and the 5 most recent documents
    exactly as they are stored in MongoDB — useful for verifying inserts work.
    """
    try:
        total     = await AppNotification.count()
        active    = await AppNotification.find(AppNotification.is_active == True).count()
        now       = datetime.utcnow()
        non_expired = await AppNotification.find(
            AppNotification.expires_at > now
        ).count()

        recent = await AppNotification.find_all().sort(
            [("created_at", -1)]
        ).limit(5).to_list()

        return {
            "collection": "app_notifications",
            "total_documents": total,
            "active_documents": active,
            "non_expired_documents": non_expired,
            "recent_5": [_serialize(n) for n in recent],
        }
    except Exception as exc:
        logger.exception(f"debug_notifications failed: {exc}")
        raise HTTPException(500, f"Debug query failed: {exc}")


# ── Admin: deactivate / delete ────────────────────────────────────────────────

@router.patch("/{notification_id}/deactivate")
async def deactivate_notification(
    notification_id: str,
    admin: User = Depends(get_current_admin_user),
):
    notif = await AppNotification.get(notification_id)
    if not notif:
        raise HTTPException(404, "Notification not found")
    notif.is_active = False
    await notif.save()
    return {"success": True, "message": "Notification deactivated"}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    admin: User = Depends(get_current_admin_user),
):
    notif = await AppNotification.get(notification_id)
    if not notif:
        raise HTTPException(404, "Notification not found")
    await notif.delete()
    return {"success": True, "message": "Notification permanently deleted"}


# ── User: fetch mine ─────────────────────────────────────────────────────────

@router.get("/my")
async def get_my_notifications(current_user: User = Depends(get_current_user)):
    uid = str(current_user.id)
    try:
        notifications = await _fetch_active_for_user(uid)
    except Exception as exc:
        logger.exception(f"get_my_notifications failed for {uid}: {exc}")
        raise HTTPException(500, "Failed to load notifications")

    return [_serialize(n, uid) for n in notifications[:50]]


@router.get("/my/unread-count")
async def get_unread_count(current_user: User = Depends(get_current_user)):
    uid = str(current_user.id)
    try:
        notifications = await _fetch_active_for_user(uid)
    except Exception as exc:
        logger.exception(f"get_unread_count failed for {uid}: {exc}")
        raise HTTPException(500, "Failed to load unread count")

    unread = sum(1 for n in notifications if uid not in (n.read_by or []))
    return {"unread_count": unread}


# ── User: mark as read ────────────────────────────────────────────────────────

@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
):
    notif = await AppNotification.get(notification_id)
    if not notif:
        raise HTTPException(404, "Notification not found")

    uid = str(current_user.id)
    read_by = notif.read_by or []
    if uid not in read_by:
        read_by.append(uid)
        notif.read_by = read_by
        await notif.save()

    return {"success": True, "read_by_count": len(notif.read_by or [])}
