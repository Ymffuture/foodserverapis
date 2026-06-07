# routes/admin_users.py
"""
Admin endpoints for user account moderation.

  GET    /admin/users                  – list / search users
  GET    /admin/users/{id}             – user detail + order stats
  POST   /admin/users/{id}/suspend     – suspend (timed or indefinite)
  POST   /admin/users/{id}/unsuspend   – lift suspension
  POST   /admin/users/{id}/ban         – permanent ban
  POST   /admin/users/{id}/unban       – lift ban
  POST   /admin/users/{id}/warn        – issue warning
  DELETE /admin/users/{id}/warnings/{idx} – remove one warning
  DELETE /admin/users/{id}             – delete account permanently
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_admin_user
from models.user import User, UserWarning
from models.order import Order

router = APIRouter(prefix="/admin/users", tags=["Admin — Users"])
logger = logging.getLogger(__name__)


# ── Schemas ──────────────────────────────────────────────────────────────────

class SuspendRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    days: Optional[int] = Field(default=None, ge=1, le=365)   # None = indefinite


class BanRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)


class WarnRequest(BaseModel):
    reason:  str            = Field(..., min_length=5, max_length=500)
    message: Optional[str] = Field(default=None, max_length=1000)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize(u: User) -> dict:
    return {
        "id":               str(u.id),
        "email":            u.email,
        "full_name":        u.full_name,
        "phone":            u.phone,
        "picture":          u.picture,
        "email_verified":   u.email_verified,
        "is_admin":         u.is_admin,
        # suspension
        "is_suspended":        u.is_suspended,
        "suspension_reason":   u.suspension_reason,
        "suspended_at":        u.suspended_at,
        "suspended_until":     u.suspended_until,
        # ban
        "is_banned":       u.is_banned,
        "banned_reason":   u.banned_reason,
        "banned_at":       u.banned_at,
        # warnings
        "warning_count":   u.warning_count,
        "warnings":        [w.model_dump() for w in (u.warnings or [])],
        # auth methods
        "has_password":    bool(u.hashed_password),
        "google_id":       u.google_id,
        "github_id":       u.github_id,
        "spotify_id":      u.spotify_id,
        # timestamps
        "created_at":      getattr(u, "created_at", None),
    }


def _guard(user: User, admin: User) -> None:
    """Raise if the target is an admin or the acting admin themselves."""
    if user.is_admin:
        raise HTTPException(403, "Cannot moderate another admin account")
    if str(user.id) == str(admin.id):
        raise HTTPException(400, "Cannot moderate your own account")


# ── List & detail ────────────────────────────────────────────────────────────

@router.get("/")
async def list_users(
    status: Optional[str] = None,  # all | active | suspended | banned | admins
    search: Optional[str] = None,
    limit:  int = 200,
    admin:  User = Depends(get_current_admin_user),
):
    query: dict = {}
    if   status == "suspended": query["is_suspended"] = True
    elif status == "banned":    query["is_banned"]    = True
    elif status == "admins":    query["is_admin"]     = True
    elif status == "active":
        query["is_suspended"] = False
        query["is_banned"]    = False

    users = await User.find(query).sort("-created_at").limit(limit).to_list()

    if search:
        sl = search.lower()
        users = [
            u for u in users
            if sl in u.email.lower() or sl in (u.full_name or "").lower()
        ]

    return [_serialize(u) for u in users]


@router.get("/{user_id}")
async def get_user_detail(
    user_id: str,
    admin: User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user:
        raise HTTPException(404, "User not found")

    orders = await Order.find(Order.user_id == user_id).to_list()
    delivered_spend = sum(
        o.total_amount or 0 for o in orders
        if (o.status.value if hasattr(o.status, "value") else str(o.status)) == "delivered"
    )

    result             = _serialize(user)
    result["order_count"]  = len(orders)
    result["total_spent"]  = round(delivered_spend, 2)
    return result


# ── Suspend ──────────────────────────────────────────────────────────────────

@router.post("/{user_id}/suspend")
async def suspend_user(
    user_id: str,
    body:    SuspendRequest,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")
    _guard(user, admin)

    now = datetime.utcnow()
    user.is_suspended       = True
    user.suspension_reason  = body.reason
    user.suspended_at       = now
    user.suspended_by       = str(admin.id)
    user.suspended_until    = (now + timedelta(days=body.days)) if body.days else None
    await user.save()

    duration = f"for {body.days} day(s)" if body.days else "indefinitely"
    logger.info(f"Admin {admin.email} suspended {user.email} {duration} — {body.reason}")
    return {"success": True, "message": f"Account suspended {duration}", "user": _serialize(user)}


@router.post("/{user_id}/unsuspend")
async def unsuspend_user(
    user_id: str,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")

    user.is_suspended      = False
    user.suspension_reason = None
    user.suspended_at      = None
    user.suspended_until   = None
    user.suspended_by      = None
    await user.save()

    logger.info(f"Admin {admin.email} lifted suspension on {user.email}")
    return {"success": True, "message": "Suspension lifted", "user": _serialize(user)}


# ── Ban ──────────────────────────────────────────────────────────────────────

@router.post("/{user_id}/ban")
async def ban_user(
    user_id: str,
    body:    BanRequest,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")
    _guard(user, admin)

    now = datetime.utcnow()
    user.is_banned        = True
    user.is_suspended     = False          # ban supersedes suspension
    user.suspension_reason = None
    user.banned_reason    = body.reason
    user.banned_at        = now
    user.banned_by        = str(admin.id)
    await user.save()

    logger.info(f"Admin {admin.email} permanently banned {user.email} — {body.reason}")
    return {"success": True, "message": "Account permanently banned", "user": _serialize(user)}


@router.post("/{user_id}/unban")
async def unban_user(
    user_id: str,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")

    user.is_banned     = False
    user.banned_reason = None
    user.banned_at     = None
    user.banned_by     = None
    await user.save()

    logger.info(f"Admin {admin.email} lifted ban on {user.email}")
    return {"success": True, "message": "Ban lifted", "user": _serialize(user)}


# ── Warn ─────────────────────────────────────────────────────────────────────

@router.post("/{user_id}/warn")
async def warn_user(
    user_id: str,
    body:    WarnRequest,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")
    _guard(user, admin)

    warning = UserWarning(
        reason=body.reason,
        message=body.message,
        issued_by_id=str(admin.id),
        issued_by_name=admin.full_name,
    )
    if not user.warnings:
        user.warnings = []
    user.warnings.append(warning)
    user.warning_count = len(user.warnings)
    await user.save()

    logger.info(f"Admin {admin.email} warned {user.email} ({user.warning_count} total) — {body.reason}")
    return {
        "success":       True,
        "message":       f"Warning issued (total: {user.warning_count})",
        "warning":       warning.model_dump(),
        "warning_count": user.warning_count,
    }


@router.delete("/{user_id}/warnings/{warning_index}")
async def delete_warning(
    user_id:       str,
    warning_index: int,
    admin: User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")
    if not user.warnings or warning_index >= len(user.warnings):
        raise HTTPException(404, "Warning not found")

    user.warnings.pop(warning_index)
    user.warning_count = len(user.warnings)
    await user.save()
    return {"success": True, "message": "Warning removed", "warning_count": user.warning_count}


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    admin:   User = Depends(get_current_admin_user),
):
    user = await User.get(user_id)
    if not user: raise HTTPException(404, "User not found")
    _guard(user, admin)

    email = user.email
    await user.delete()
    logger.info(f"Admin {admin.email} permanently deleted account: {email}")
    return {"success": True, "message": f"Account '{email}' permanently deleted"}
