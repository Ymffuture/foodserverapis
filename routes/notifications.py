# routes/notifications.py
"""
Notification system — admin broadcasts or targets a single user.

Admin:
  POST   /notifications              – create notification
  GET    /notifications/admin/all    – list all (admin view with read counts)
  PATCH  /notifications/{id}/deactivate
  DELETE /notifications/{id}

Users (customer-facing app):
  GET    /notifications/my           – unread notifications for me
  PATCH  /notifications/{id}/read    – mark one as read
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_admin_user, get_current_user
from models.user import User
from models.notification import AppNotification, NotificationType, NotificationTarget

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
    return {
        "id":              str(n.id),
        "title":           n.title,
        "message":         n.message,
        "type":            n.type.value,
        "target":          n.target.value,
        "target_user_id":  n.target_user_id,
        "created_by":      n.created_by,
        "created_by_name": n.created_by_name,
        "is_active":       n.is_active,
        "read_by_count":   len(n.read_by),
        "is_read":         (viewer_id in n.read_by) if viewer_id else False,
        "created_at":      n.created_at,
        "expires_at":      n.expires_at,
    }


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

    notif = AppNotification(
        title=body.title,
        message=body.message,
        type=body.type,
        target=body.target,
        target_user_id=body.target_user_id,
        created_by=str(admin.id),
        created_by_name=admin.full_name,
        expires_at=expires,
    )
    await notif.insert()

    logger.info(f"Notification '{body.title}' created by {admin.email} → {body.target.value}")
    return {"success": True, "message": "Notification sent", "notification": _serialize(notif)}


# ── Admin: list all ──────────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_notifications(
    limit:       int  = 100,
    active_only: bool = False,
    admin: User = Depends(get_current_admin_user),
):
    query: dict = {}
    if active_only:
        query["is_active"] = True

    notifications = await AppNotification.find(query).sort("-created_at").limit(limit).to_list()
    return [_serialize(n) for n in notifications]


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
    now = datetime.utcnow()
    uid = str(current_user.id)

    notifications = await AppNotification.find({
        "is_active": True,
        "expires_at": {"$gt": now},
        "$or": [
            {"target": NotificationTarget.ALL.value},
            {"target": NotificationTarget.SPECIFIC.value, "target_user_id": uid},
        ],
    }).sort("-created_at").limit(50).to_list()

    return [_serialize(n, uid) for n in notifications]


@router.get("/my/unread-count")
async def get_unread_count(current_user: User = Depends(get_current_user)):
    now = datetime.utcnow()
    uid = str(current_user.id)

    all_notifs = await AppNotification.find({
        "is_active": True,
        "expires_at": {"$gt": now},
        "$or": [
            {"target": NotificationTarget.ALL.value},
            {"target": NotificationTarget.SPECIFIC.value, "target_user_id": uid},
        ],
    }).to_list()

    unread = sum(1 for n in all_notifs if uid not in n.read_by)
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
    if uid not in notif.read_by:
        notif.read_by.append(uid)
        await notif.save()

    return {"success": True, "read_by_count": len(notif.read_by)}
