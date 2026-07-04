# routes/users.py
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from datetime import datetime
from typing import Optional
from models.user import User
from dependencies import get_current_user, get_password_hash, verify_password
from schemas.user_schema import UserProfileUpdate, PasswordChangeRequest
from services.cloudinary_service import upload_image

router = APIRouter(prefix="/users", tags=["Users"])

# ── Social link domains — auto-prefixed if the user enters a bare handle ──
SOCIAL_DOMAINS = {
    "facebook":  "https://facebook.com/",
    "github":    "https://github.com/",
    "x":         "https://x.com/",
    "instagram": "https://instagram.com/",
}


def _normalize_social_url(platform: str, value: Optional[str]) -> Optional[str]:
    """Lets a user type either a full URL or a bare handle ('@username' or
    'username') and always stores a real, openable URL."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return ""  # explicit clear
    if value.startswith("http://") or value.startswith("https://"):
        return value
    handle = value.lstrip("@")
    return f"{SOCIAL_DOMAINS[platform]}{handle}"


FEATURE_PERMISSIONS = {
    "active": {
        "canAddToCart":  True,
        "canCheckout":   True,
        "canOrder":      True,
        "canUseWallet":  True,
        "canUseRewards": True,
        "canChat":       True,
        "canViewOrders": True,
    },
    "warned": {
        "canAddToCart":  True,
        "canCheckout":   True,
        "canOrder":      True,
        "canUseWallet":  True,
        "canUseRewards": True,
        "canChat":       True,
        "canViewOrders": True,
    },
    "restricted": {
        "canAddToCart":  False,
        "canCheckout":   False,
        "canOrder":      False,
        "canUseWallet":  False,
        "canUseRewards": False,
        "canChat":       True,
        "canViewOrders": True,
    },
    "suspended": {
        "canAddToCart":  False,
        "canCheckout":   False,
        "canOrder":      False,
        "canUseWallet":  False,
        "canUseRewards": False,
        "canChat":       False,
        "canViewOrders": True,
    },
    "banned": {
        "canAddToCart":  False,
        "canCheckout":   False,
        "canOrder":      False,
        "canUseWallet":  False,
        "canUseRewards": False,
        "canChat":       False,
        "canViewOrders": False,
    },
}


def _derive_status(user: User) -> dict:
    now = datetime.utcnow()

    if user.is_banned:
        status     = "banned"
        reason     = user.banned_reason
        expires_at = None

    elif user.is_suspended:
        # Auto-lift if timed suspension has passed
        if user.suspended_until and now > user.suspended_until:
            status     = "active"
            reason     = None
            expires_at = None
        else:
            status     = "suspended"
            reason     = user.suspension_reason
            expires_at = (
                user.suspended_until.isoformat()
                if user.suspended_until else None
            )

    elif user.warning_count >= 3:
        status     = "restricted"
        reason     = f"Account restricted after {user.warning_count} warnings."
        expires_at = None

    elif user.warning_count > 0:
        status     = "warned"
        reason     = user.warnings[-1].reason if user.warnings else None
        expires_at = None

    else:
        status     = "active"
        reason     = None
        expires_at = None

    features          = FEATURE_PERMISSIONS.get(status, FEATURE_PERMISSIONS["active"])
    affected_features = [k for k, v in features.items() if not v]

    return {
        "status":            status,
        "reason":            reason,
        "expires_at":        expires_at,
        "affected_features": affected_features,
        "warning_count":     user.warning_count,
        "appealed":          False,
    }


@router.get("/me/status")
async def get_my_status(current_user: User = Depends(get_current_user)):
    return _derive_status(current_user)


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id":             str(current_user.id),
        "email":          current_user.email,
        "full_name":      current_user.full_name,
        "phone":          current_user.phone,
        "address":        current_user.address,
        "social_links":   current_user.social_links.model_dump(),
        "picture":        current_user.picture,
        "email_verified": current_user.email_verified,
        "is_admin":       current_user.is_admin,
        "has_password":   bool(current_user.hashed_password),
        "plan":           current_user.plan,
        **_derive_status(current_user),
    }


@router.patch("/me")
async def update_me(
    body: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update profile fields — phone, address, full name, social links.
    Only fields present in the request are touched."""
    if body.full_name is not None:
        current_user.full_name = body.full_name
    if body.phone is not None:
        current_user.phone = body.phone or None
    if body.address is not None:
        current_user.address = body.address.strip() or None
    if body.social_links is not None:
        links = current_user.social_links
        for platform in ("facebook", "github", "x", "instagram"):
            raw = getattr(body.social_links, platform)
            if raw is not None:
                normalized = _normalize_social_url(platform, raw)
                setattr(links, platform, normalized or None)
        current_user.social_links = links

    await current_user.save()

    return {
        "id":           str(current_user.id),
        "full_name":    current_user.full_name,
        "phone":        current_user.phone,
        "address":      current_user.address,
        "social_links": current_user.social_links.model_dump(),
        "picture":      current_user.picture,
    }


@router.post("/me/avatar")
async def update_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Upload/replace the profile picture. Overwrites any OAuth-provided
    picture (Google/GitHub/etc.) with the user's own upload."""
    if file.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"):
        raise HTTPException(status_code=422, detail="Please upload a JPG, PNG, WEBP, or GIF image.")

    url = await upload_image(file)
    current_user.picture = url
    await current_user.save()
    return {"picture": url}


@router.post("/me/password")
async def change_password(
    body: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
):
    """Change password. OAuth-only accounts (hashed_password is None) can
    set a password for the first time without providing current_password;
    everyone else must confirm their current password first."""
    if current_user.hashed_password:
        if not body.current_password or not verify_password(body.current_password, current_user.hashed_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
        if verify_password(body.new_password, current_user.hashed_password):
            raise HTTPException(status_code=422, detail="New password must be different from your current password.")

    current_user.hashed_password = get_password_hash(body.new_password)
    await current_user.save()
    return {"message": "Password updated successfully."}
