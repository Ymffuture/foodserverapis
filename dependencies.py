# dependencies.py
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from models.user import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ── Password helpers ──────────────────────────────────────────────────────────

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ── Status helpers (mirrors routes/Users.py — single source of truth) ─────────

# Must stay in sync with FEATURE_PERMISSIONS in routes/Users.py.
# Defined here so get_current_active_user can build the 423 payload without
# importing from routes (which would create a circular dependency).
_FEATURE_PERMISSIONS = {
    "active":     {
        "canAddToCart": True,  "canCheckout": True,  "canOrder": True,
        "canUseWallet": True,  "canUseRewards": True, "canChat": True,
        "canViewOrders": True,
    },
    "warned":     {
        "canAddToCart": True,  "canCheckout": True,  "canOrder": True,
        "canUseWallet": True,  "canUseRewards": True, "canChat": True,
        "canViewOrders": True,
    },
    "restricted": {
        "canAddToCart": False, "canCheckout": False, "canOrder": False,
        "canUseWallet": False, "canUseRewards": False, "canChat": True,
        "canViewOrders": True,
    },
    "suspended":  {
        "canAddToCart": False, "canCheckout": False, "canOrder": False,
        "canUseWallet": False, "canUseRewards": False, "canChat": False,
        "canViewOrders": True,
    },
    "banned":     {
        "canAddToCart": False, "canCheckout": False, "canOrder": False,
        "canUseWallet": False, "canUseRewards": False, "canChat": False,
        "canViewOrders": False,
    },
}


def _derive_status_payload(user: User) -> dict:
    """
    Build the account-status dict from a User document.
    Returns the same shape as GET /users/me/status so the frontend
    UserStatusContext can consume it from both sources.
    """
    now = datetime.utcnow()

    if user.is_banned:
        acct_status = "banned"
        reason      = user.banned_reason
        expires_at  = None

    elif user.is_suspended:
        if user.suspended_until and now > user.suspended_until:
            # Timed suspension has expired — treat as active.
            # The next explicit call to /users/me/status will persist the lift.
            acct_status = "active"
            reason      = None
            expires_at  = None
        else:
            acct_status = "suspended"
            reason      = user.suspension_reason
            expires_at  = (
                user.suspended_until.isoformat()
                if user.suspended_until else None
            )

    elif user.warning_count >= 3:
        acct_status = "restricted"
        reason      = f"Account restricted after {user.warning_count} warnings."
        expires_at  = None

    elif user.warning_count > 0:
        acct_status = "warned"
        reason      = user.warnings[-1].reason if user.warnings else None
        expires_at  = None

    else:
        acct_status = "active"
        reason      = None
        expires_at  = None

    features          = _FEATURE_PERMISSIONS.get(acct_status, _FEATURE_PERMISSIONS["active"])
    affected_features = [k for k, v in features.items() if not v]

    return {
        "status":            acct_status,
        "reason":            reason,
        "expires_at":        expires_at,
        "affected_features": affected_features,
        "warning_count":     user.warning_count,
        "appealed":          False,
    }


# ── Auth dependencies ─────────────────────────────────────────────────────────

async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = await User.find_one(User.email == email)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Extends get_current_user with account-status enforcement.

    Raises HTTP 423 (Locked) for banned or suspended users, embedding
    the full account_status payload in the response body so the frontend
    axios interceptor in UserStatusContext can update the UI immediately
    without needing a separate /users/me/status poll.

    Endpoints that must enforce feature gating (checkout, wallet, rewards)
    should depend on this instead of get_current_user.

    Example:
        @router.post("/checkout")
        async def checkout(user: User = Depends(get_current_active_user)):
            ...
    """
    status_data = _derive_status_payload(current_user)

    if status_data["status"] in ("banned", "suspended"):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "message":           "Account access restricted.",
                "account_status":    status_data["status"],
                "reason":            status_data["reason"],
                "expires_at":        status_data["expires_at"],
                "affected_features": status_data["affected_features"],
            },
        )

    return current_user


async def get_current_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
