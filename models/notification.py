# models/notification.py
from beanie import Document
from pydantic import Field
from datetime import datetime, timedelta
from typing import Optional, List
from enum import Enum


class NotificationType(str, Enum):
    INFO        = "info"
    WARNING     = "warning"
    MAINTENANCE = "maintenance"
    PROMO       = "promo"
    UPDATE      = "update"
    URGENT      = "urgent"


class NotificationTarget(str, Enum):
    ALL      = "all"       # broadcast to every user
    SPECIFIC = "specific"  # single user only


def _default_expires() -> datetime:
    return datetime.utcnow() + timedelta(days=30)


class AppNotification(Document):
    title:   str
    message: str
    type:    NotificationType    = NotificationType.INFO
    target:  NotificationTarget  = NotificationTarget.ALL
    target_user_id: Optional[str] = None   # set only when target == SPECIFIC

    # Admin who sent it
    created_by:      str          # admin user_id
    created_by_name: str

    # Lifecycle
    is_active: bool            = True
    read_by:   List[str]       = Field(default_factory=list)   # user_ids who read/dismissed

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime = Field(default_factory=_default_expires)

    class Settings:
        name = "app_notifications"
        indexes = [
            "created_by",
            "target",
            "is_active",
            "created_at",
            [("target", 1), ("is_active", 1)],
            [("target_user_id", 1), ("is_active", 1)],
        ]
