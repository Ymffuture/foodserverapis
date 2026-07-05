# routes/referrals.py
from fastapi import APIRouter, Depends
from models.user import User
from dependencies import get_current_user
from services.referral_service import generate_referral_code, REFERRAL_BONUS_POINTS

router = APIRouter(prefix="/referrals", tags=["Referrals"])


@router.get("/me")
async def get_my_referrals(current_user: User = Depends(get_current_user)):
    """
    Returns the current user's referral code + stats. Generates a code
    lazily for accounts created before this feature existed (registration
    now always sets one up front).
    """
    if not current_user.referral_code:
        current_user.referral_code = await generate_referral_code(current_user.full_name)
        await current_user.save()

    referred_users = await User.find(User.referred_by == str(current_user.id)).to_list()
    converted = [u for u in referred_users if u.referral_reward_granted]

    return {
        "referral_code":       current_user.referral_code,
        "bonus_points_earned": current_user.referral_bonus_points,
        "bonus_per_referral":  REFERRAL_BONUS_POINTS,
        "total_referred":      len(referred_users),
        "total_converted":     len(converted),  # referred users whose first order was actually delivered
        "pending":             len(referred_users) - len(converted),
    }
