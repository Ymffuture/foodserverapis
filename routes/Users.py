# routes/users.py
from fastapi import APIRouter, Depends
from datetime import datetime
from models.user import User
from dependencies import get_current_user

router = APIRouter(prefix="/users", tags=["Users"])

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
        "picture":        current_user.picture,
        "email_verified": current_user.email_verified,
        "is_admin":       current_user.is_admin,
        **_derive_status(current_user),
    }
