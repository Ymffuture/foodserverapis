# services/referral_service.py
import random
import string
from models.user import User
from models.order import Order
from utils.enums import OrderStatus

REFERRAL_BONUS_POINTS = 50  # KotaPoints credited to BOTH referrer and referee


def _slug(name: str) -> str:
    base = "".join(ch for ch in (name or "").upper() if ch.isalnum())[:6]
    return base or "KOTA"


async def generate_referral_code(full_name: str) -> str:
    """Generate a unique, human-shareable referral code, e.g. THABOB4821."""
    base = _slug(full_name)
    for _ in range(20):
        suffix = "".join(random.choices(string.digits, k=4))
        code = f"{base}{suffix}"
        if not await User.find_one(User.referral_code == code):
            return code
    raise RuntimeError("Could not generate a unique referral code after 20 attempts")


async def apply_referral_code_at_signup(new_user: User, code: str) -> bool:
    """
    Link new_user to whoever owns `code`. Mutates new_user.referred_by in
    place — caller is responsible for saving/inserting. Returns True if a
    valid referrer was found and linked. No points are granted here —
    that only happens once the referee's first order is actually delivered,
    so referrals can't be farmed with throwaway signups.
    """
    code = (code or "").strip().upper()
    if not code:
        return False

    referrer = await User.find_one(User.referral_code == code)
    if not referrer:
        return False
    # Guard against self-referral (shouldn't be reachable pre-insert, but cheap to check)
    if new_user.email and referrer.email == new_user.email:
        return False

    new_user.referred_by = str(referrer.id)
    return True


async def apply_referral_reward_if_eligible(order: Order) -> None:
    """
    Call this whenever an order transitions to DELIVERED (see
    routes/orders.py:update_order_status). If this is the referred user's
    FIRST delivered order, credits REFERRAL_BONUS_POINTS to both the
    referrer and the referee, exactly once.
    """
    if order.status != OrderStatus.DELIVERED.value:
        return

    referee = await User.get(order.user_id)
    if not referee or not referee.referred_by or referee.referral_reward_granted:
        return

    delivered_count = await Order.find({
        "user_id": order.user_id,
        "status": OrderStatus.DELIVERED.value,
    }).count()
    if delivered_count != 1:
        return  # not their first delivered order — reward already should have fired earlier

    referrer = await User.get(referee.referred_by)
    if not referrer:
        return

    referee.referral_bonus_points += REFERRAL_BONUS_POINTS
    referee.referral_reward_granted = True
    referrer.referral_bonus_points += REFERRAL_BONUS_POINTS

    await referee.save()
    await referrer.save()
