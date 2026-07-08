# routes/appeals.py
"""
User-facing appeal submission + admin review.

User:
  POST  /appeals/          – submit an appeal (one active appeal per user)
  GET   /appeals/my        – get own appeal status

Admin:
  GET   /appeals/          – list all appeals (filter by status)
  POST  /appeals/{id}/review – approve or reject
"""
import logging
from datetime import datetime
from typing import Optional

from beanie import Document
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_user, get_current_admin_user
from models.user import User

router = APIRouter(prefix="/appeals", tags=["Appeals"])
logger = logging.getLogger(__name__)

VALID_STATUSES = {"active", "warned", "restricted", "suspended", "banned"}


# ── Beanie document ───────────────────────────────────────────────────────────

class AppealDoc(Document):
    user_id:               str
    user_email:            str
    user_name:             str
    category:              str             # wrong_decision | misunderstanding | reformed | technical_error | other
    reason:                str
    account_status_at_time: str            # status snapshot at submission
    status:                str  = "pending"  # pending | approved | rejected
    admin_note:            Optional[str]  = None
    reviewed_by:           Optional[str]  = None
    reviewed_at:           Optional[datetime] = None
    created_at:            datetime       = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "appeals"


# ── Schemas ───────────────────────────────────────────────────────────────────

class SubmitAppealIn(BaseModel):
    category:       str  = Field(..., pattern="^(wrong_decision|misunderstanding|reformed|technical_error|other)$")
    reason:         str  = Field(..., min_length=80, max_length=1000)
    account_status: str  = Field(..., pattern="^(warned|restricted|suspended|banned)$")


class ReviewAppealIn(BaseModel):
    decision:           str           = Field(..., pattern="^(approved|rejected)$")
    admin_note:         Optional[str] = Field(None, max_length=500)
    # Only meaningful when decision == "approved". Defaults to True — an
    # approved appeal almost always means the restriction should be lifted;
    # this exists mainly so a future admin UI can approve an appeal purely
    # as a record without touching the account, if that's ever needed.
    clear_restrictions: bool          = True


def _serialize(a: AppealDoc) -> dict:
    return {
        "id":                     str(a.id),
        "user_id":                a.user_id,
        "user_email":             a.user_email,
        "user_name":              a.user_name,
        "category":               a.category,
        "reason":                 a.reason,
        "account_status_at_time": a.account_status_at_time,
        "status":                 a.status,
        "admin_note":             a.admin_note,
        "reviewed_by":            a.reviewed_by,
        "reviewed_at":            a.reviewed_at,
        "created_at":             a.created_at,
    }


# ── User endpoints ────────────────────────────────────────────────────────────

@router.post("/", status_code=201)
async def submit_appeal(
    body:         SubmitAppealIn,
    current_user: User = Depends(get_current_user),
):
    # One pending appeal at a time
    existing = await AppealDoc.find_one({
        "user_id": str(current_user.id),
        "status":  "pending",
    })
    if existing:
        raise HTTPException(409, "You already have a pending appeal under review.")

    appeal = AppealDoc(
        user_id=str(current_user.id),
        user_email=current_user.email,
        user_name=current_user.full_name or current_user.email,
        category=body.category,
        reason=body.reason,
        account_status_at_time=body.account_status,
    )
    await appeal.insert()

    logger.info(
        f"Appeal submitted | user={current_user.email} "
        f"| status={body.account_status} | category={body.category}"
    )
    return {"success": True, "message": "Appeal submitted — we'll be in touch within 24–48 hrs.", "appeal": _serialize(appeal)}


@router.get("/my")
async def get_my_appeal(current_user: User = Depends(get_current_user)):
    """Returns the most recent appeal for the authenticated user."""
    appeal = await AppealDoc.find_one(
        {"user_id": str(current_user.id)},
        sort=[("created_at", -1)],
    )
    if not appeal:
        return {"appeal": None}
    return {"appeal": _serialize(appeal)}


# ── Admin endpoints ───────────────────────────────────────────────────────────

@router.get("/")
async def list_appeals(
    status:         Optional[str] = None,   # pending | approved | rejected
    account_status: Optional[str] = None,   # warned | restricted | suspended | banned
    limit:          int           = 100,
    admin:          User          = Depends(get_current_admin_user),
):
    query = {}
    if status in ("pending", "approved", "rejected"):
        query["status"] = status
    if account_status in ("warned", "restricted", "suspended", "banned"):
        query["account_status_at_time"] = account_status

    appeals = await AppealDoc.find(query).sort([("created_at", -1)]).limit(limit).to_list()
    return [_serialize(a) for a in appeals]


@router.post("/{appeal_id}/review")
async def review_appeal(
    appeal_id: str,
    body:      ReviewAppealIn,
    admin:     User = Depends(get_current_admin_user),
):
    appeal = await AppealDoc.get(appeal_id)
    if not appeal:
        raise HTTPException(404, "Appeal not found")
    if appeal.status != "pending":
        raise HTTPException(400, f"Appeal already {appeal.status}")

    appeal.status      = body.decision
    appeal.admin_note  = body.admin_note
    appeal.reviewed_by = str(admin.id)
    appeal.reviewed_at = datetime.utcnow()
    await appeal.save()

    # If approved — clear EVERYTHING on the account, not just whichever
    # single status this appeal happened to be filed against. A user could
    # be banned *and* still be carrying old warnings from before that; an
    # approved appeal means a clean slate, so unban, unsuspend, AND wipe
    # the warning history together, unconditionally. (Previously this only
    # cleared the one matching `account_status_at_time`, and silently did
    # nothing at all for "warned" appeals — both fixed here.)
    if body.decision == "approved" and body.clear_restrictions:
        user = await User.get(appeal.user_id)
        if user:
            user.is_banned         = False
            user.banned_reason     = None
            user.banned_at         = None
            user.banned_by         = None

            user.is_suspended      = False
            user.suspension_reason = None
            user.suspended_until   = None
            user.suspended_at      = None
            user.suspended_by      = None

            user.warnings          = []
            user.warning_count     = 0

            await user.save()
            logger.info(
                f"Appeal {appeal_id} approved by {admin.email} — "
                f"ALL restrictions cleared for {user.email} "
                f"(was {appeal.account_status_at_time})"
            )

    logger.info(f"Appeal {appeal_id} {body.decision} by {admin.email}")
    return {"success": True, "decision": body.decision, "appeal": _serialize(appeal)}
