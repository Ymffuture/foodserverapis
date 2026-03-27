# routes/ai.py
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from openai import AsyncOpenAI

from dependencies import get_current_user, get_current_admin_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion
from models.delivery_driver import DeliveryDriver, DriverStatus
from models.delivery_assignment import DeliveryAssignment, AssignmentStatus
from models.wallet_transaction import WalletTransaction
from utils.enums import OrderStatus
from utils.business_hours import get_status


router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── OpenRouter Setup ───────────────────────────────────────────────────────
KIMI_API_KEY = os.getenv("KIMI_API_KEY")
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

client: Optional[AsyncOpenAI] = None
if KIMI_API_KEY:
    client = AsyncOpenAI(
        api_key=KIMI_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://foodsorder.vercel.app",
            "X-Title": "KotaBites",
        },
    )

MAX_HISTORY_TURNS = 100
CANCELLABLE_STATUSES = {OrderStatus.PENDING, OrderStatus.PAID}

# SAST timezone (UTC+2, no DST)
SAST = timezone(timedelta(hours=2))

# How many minutes after closing KotaBot stays silent
POST_CLOSE_GRACE_MINUTES = 30


# ── Schemas ────────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    order_id: Optional[str] = None


class SuggestionRequest(BaseModel):
    message: str = Field(..., min_length=5, max_length=2000)
    category: Optional[str] = Field(default="general", max_length=50)


class CancelOrderRequest(BaseModel):
    order_id: str = Field(..., min_length=24, max_length=24)
    reason: Optional[str] = Field(default=None, max_length=500)


# ── Time helpers ───────────────────────────────────────────────────────────
def _now_sast() -> datetime:
    return datetime.now(SAST)


def _sast_label() -> str:
    """Human-readable current SAST time, e.g.  'Friday 17:42 SAST'."""
    now = _now_sast()
    return now.strftime("%A %d %B %Y · %H:%M SAST")


def _is_ai_active(hours_status: dict) -> tuple[bool, str]:
    """
    Returns (active, reason_string).
    KotaBot goes silent 30 minutes AFTER the delivery window closes.
    This gives customers a grace period to ask post-order questions.
    """
    if hours_status["is_open"]:
        return True, ""

    # If never opened today (e.g. Sunday) check directly
    close_time_str = hours_status.get("close_time")  # e.g. "17:00"
    if not close_time_str:
        # Completely closed day — no grace window
        return False, hours_status.get("message", "We are closed today.")

    now = _now_sast()
    ch, cm = map(int, close_time_str.split(":"))
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)

    minutes_since_close = int((now - close_dt).total_seconds() / 60)

    if minutes_since_close <= POST_CLOSE_GRACE_MINUTES:
        remaining = POST_CLOSE_GRACE_MINUTES - minutes_since_close
        return True, f"[POST-CLOSE GRACE — {remaining} min remaining. Only answer order/tracking questions, no new orders.]"

    # Fully silent
    next_msg = hours_status.get("message", "We are currently closed.")
    return False, next_msg


# ── Driver context builder ─────────────────────────────────────────────────
async def _build_driver_block(user_id: str) -> str:
    try:
        driver = await DeliveryDriver.find_one(DeliveryDriver.user_id == user_id)
        if not driver:
            return ""

        status_label = {
            "pending":   "Under Review (waiting admin approval)",
            "approved":  "Active Driver ✅",
            "active":    "Active Driver ✅",
            "offline":   "Offline (approved but not currently working)",
            "rejected":  "Rejected ❌",
            "suspended": "Suspended ⚠️",
        }.get(driver.status.value, driver.status.value)

        availability = "🟢 Online — accepting orders" if driver.is_available else "🔴 Offline"

        # Active delivery
        active_delivery_block = ""
        if driver.current_order_id:
            assignment = await DeliveryAssignment.find_one(
                DeliveryAssignment.order_id == driver.current_order_id,
                DeliveryAssignment.driver_id == str(driver.id),
            )
            if assignment:
                step_map = {
                    "accepted":   "🟡 Accepted — heading to restaurant",
                    "picked_up":  "🔵 Picked up — food collected",
                    "in_transit": "🟠 In transit — on the way to customer",
                    "delivered":  "🟢 Delivered — completed",
                }
                delivery_status = step_map.get(assignment.status.value, assignment.status.value)
                mins_on_road = ""
                if assignment.accepted_at:
                    elapsed = int((_now_sast() - assignment.accepted_at.replace(tzinfo=SAST)).total_seconds() / 60)
                    mins_on_road = f" ({elapsed} min ago)"
                active_delivery_block = f"""
  ┌─ ACTIVE DELIVERY ────────────────────────────────
  │ Order       : #{str(assignment.order_id)[-8:].upper()} (ID: {assignment.order_id})
  │ Status      : {delivery_status}
  │ Accepted at : {assignment.accepted_at.strftime('%H:%M SAST') if assignment.accepted_at else 'N/A'}{mins_on_road}
  │ Picked up   : {assignment.picked_up_at.strftime('%H:%M SAST') if assignment.picked_up_at else 'Not yet'}
  │ Customer    : {assignment.customer_name} · {assignment.customer_phone or 'no phone'}
  │ Address     : {assignment.delivery_address}
  │ Delivery fee: R{assignment.delivery_fee:.2f}
  └──────────────────────────────────────────────────"""

        recent_tx = await WalletTransaction.find(
            WalletTransaction.driver_id == str(driver.id)
        ).sort("-created_at").limit(5).to_list()

        tx_lines = ""
        if recent_tx:
            tx_lines = "\nRecent wallet transactions:\n" + "\n".join(
                f"  {t.created_at.strftime('%d %b %Y %H:%M')} | "
                f"{'+'if t.amount>0 else ''}R{t.amount:.2f} | "
                f"{t.type.value:20} | Bal: R{t.balance_after:.2f} | {t.description[:40]}"
                for t in recent_tx
            )

        return f"""
╔══════════════════════════════════════════════════════════════╗
║                  THIS USER IS ALSO A DRIVER                  ║
╚══════════════════════════════════════════════════════════════╝
Full name        : {driver.full_name}
Email            : {driver.email}
Phone            : {driver.phone}
Status           : {status_label}
Availability     : {availability}
Vehicle          : {driver.vehicle_type.value.capitalize()}
Rating           : {driver.rating:.1f} / 5.0  ({driver.total_ratings} ratings)
Total deliveries : {driver.total_deliveries}

── Wallet ──────────────────────────────────────────
Balance          : R{driver.wallet_balance:.2f}
Total earned     : R{driver.total_earned:.2f}
Total withdrawn  : R{driver.total_withdrawn:.2f}
Min withdrawal   : R50.00  (processed in 24–48 hrs)
{tx_lines}
{active_delivery_block}

── Driver Behaviour Rules ──────────────────────────
- Address them as a driver when relevant
- Help with wallet, earnings, delivery stats
- If pending: remind them approval takes up to 24 hours
- If rejected/suspended: direct them to Kgomotso at 065 393 5339
- Withdrawals: minimum R50, 24–48 hr processing
- Going online/offline: Driver Dashboard → toggle availability
- Accepting orders: they appear in Driver Dashboard → Orders tab
- Delivery steps: Accepted → Picked Up → In Transit → Delivered
- They still order food too — help with customer questions as well
"""
    except Exception as e:
        logger.warning(f"Driver block build failed for user {user_id}: {e}")
        return ""


# ── System Prompt ──────────────────────────────────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:

    hours_status = get_status()
    now_sast     = _now_sast()
    current_time = _sast_label()

    ai_active, ai_status_note = _is_ai_active(hours_status)

    if hours_status["is_open"]:
        hours_block = (
            f"DELIVERY: OPEN ✅ — closes at {hours_status['close_time']} SAST today ({hours_status['day']})"
        )
    else:
        hours_block = f"DELIVERY: CLOSED 🔴 — {hours_status['message']}"

    # ── Menu ──
    try:
        items = await MenuItem.find_all().to_list(length=60)
        menu_text = "\n".join(
            f"  • {i.name:<30} R{i.price:>6.2f}  [{i.category}]"
            + (f"\n    {i.description[:100]}" if i.description else "")
            for i in items
        ) or "  (Menu currently empty)"
    except Exception:
        menu_text = "  (Menu unavailable)"

    # ── Active order context ──
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str  = ", ".join(f"{it.name} ×{it.quantity}" for it in (order.items or []))
                status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
                can_cancel = order.status in CANCELLABLE_STATUSES

                # Try to fetch driver info for this order
                driver_assignment = None
                if order.status not in ["pending", "cancelled"]:
                    try:
                        assignments = await DeliveryAssignment.find({
                            "order_id": str(order.id),
                            "status": {"$in": [
                                AssignmentStatus.ACCEPTED.value,
                                AssignmentStatus.PICKED_UP.value,
                                AssignmentStatus.IN_TRANSIT.value,
                                AssignmentStatus.DELIVERED.value,
                            ]}
                        }).to_list()
                        if assignments:
                            driver_assignment = assignments[0]
                    except Exception:
                        pass

                driver_info_line = ""
                if driver_assignment:
                    step_map = {
                        "accepted":   "Driver heading to restaurant",
                        "picked_up":  "Food collected — driver on the way",
                        "in_transit": "Driver in transit to you RIGHT NOW 🛵",
                        "delivered":  "Delivered ✅",
                    }
                    driver_info_line = (
                        f"\nDriver      : {driver_assignment.driver_name} · {driver_assignment.driver_phone}"
                        f"\nDelivery    : {step_map.get(driver_assignment.status.value, driver_assignment.status.value)}"
                        f"\nDelivery fee: R{driver_assignment.delivery_fee:.2f}"
                    )

                order_block = f"""
╔═══════════════════════════════════════════════════╗
║                  ACTIVE ORDER                      ║
╚═══════════════════════════════════════════════════╝
Order #{str(order.id)[-8:].upper()} (full ID: {str(order.id)})
Status      : {status_val.upper()}
Total       : R{order.total_amount:.2f}
Items       : {items_str or 'none'}
Payment     : {order.payment_method or 'paystack'}
Address     : {order.delivery_address or 'Not specified'}
Phone       : {order.phone or 'Not provided'}
Cancellable : {'YES (still ' + status_val + ')' if can_cancel else 'NO (already ' + status_val + ')'}
{driver_info_line}
"""
        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    # ── Order history ──
    history_block = ""
    try:
        recent = await Order.find(Order.user_id == str(user.id)).to_list(length=15)
        if recent:
            recent.sort(key=lambda o: o.created_at, reverse=True)
            total_spent  = sum(o.total_amount for o in recent)
            delivered_total = sum(
                o.total_amount for o in recent
                if (o.status.value if hasattr(o.status, "value") else str(o.status)) == "delivered"
            )
            lines = []
            for o in recent:
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                items_str  = ", ".join(f"{it.name} ×{it.quantity}" for it in (o.items or []))
                can_cancel = o.status in CANCELLABLE_STATUSES
                lines.append(
                    f"  #{str(o.id)[-8:].upper()} (ID:{str(o.id)}) | "
                    f"{status:10} | R{o.total_amount:>7.2f} | "
                    f"{o.created_at.strftime('%d %b %Y %H:%M')} | "
                    f"{items_str} | {'can cancel' if can_cancel else 'locked'}"
                )
            history_block = (
                f"=== ORDER HISTORY ({len(recent)} orders | R{total_spent:.2f} total spent | R{delivered_total:.2f} delivered) ===\n"
                + "\n".join(lines)
            )
        else:
            history_block = "=== ORDER HISTORY ===\nNo previous orders yet."
    except Exception as e:
        logger.warning(f"Order history fetch failed: {e}")

    driver_block = await _build_driver_block(str(user.id))
    phone = getattr(user, "phone", None) or "Not on file"

    # ── AI availability note ──
    if not ai_active:
        ai_availability_block = f"""
╔══════════════════════════════════════════════════════╗
║  ⛔  KOTABOT IS NOW IN SILENT MODE                    ║
║  Delivery closed more than {POST_CLOSE_GRACE_MINUTES} minutes ago.           ║
║  DO NOT answer new food/order questions.             ║
║  You MAY still: greet, explain when we reopen, help  ║
║  with existing order tracking only.                  ║
╚══════════════════════════════════════════════════════╝
{ai_status_note}
"""
    elif ai_status_note:
        ai_availability_block = f"\n[NOTE: {ai_status_note}]\n"
    else:
        ai_availability_block = ""

    return f"""You are KotaBot, the AI assistant for KotaBites — Johannesburg's favourite kota sandwich delivery service.

══════════════════════════════════════════════════════
  🕐 CURRENT TIME (SAST)  :  {current_time}
  📍 LOCATION             :  Tjovitjo Phase 2, Johannesburg, South Africa
  {hours_block}
══════════════════════════════════════════════════════
{ai_availability_block}

═══════════════════════════════════════════════════════════
                    ABOUT KOTABITES
═══════════════════════════════════════════════════════════
KotaBites is an online kota sandwich delivery platform based in
Johannesburg South, South Africa. Customers order fresh, affordable
kota sandwiches via the website and a driver delivers within a
1.3 km radius of the kitchen.

Owner / Founder  : Kgomotso Nkosi
Email            : futurekgomotso@gmail.com
Phone            : 065 393 5339
Website          : https://foodsorder.vercel.app
Admin panel      : (internal, admin login required)
Backend API docs : https://kotabites.onrender.com/docs

Tech stack:
  Frontend : React 19 + Vite + TailwindCSS (deployed on Vercel)
  Backend  : FastAPI + MongoDB (Beanie ODM, deployed on Render)
  Payments : Paystack (card, EFT, instant EFT)
  Images   : Cloudinary
  AI       : OpenRouter (nvidia/nemotron-3-super)
  Email    : EmailJS (no SMTP needed)

Key app features:
  ✅ Menu browsing with 3D card viewer
  ✅ Cart, checkout, Paystack & cash on delivery
  ✅ Real-time order tracking (5s polling)
  ✅ KotaBot AI assistant (this is you!)
  ✅ Live delivery tracker banner on menu page
  ✅ Driver dashboard (accept orders, delivery steps, wallet)
  ✅ Customer rewards wallet (KotaPoints, redeem codes)
  ✅ Delivery coverage map (React Leaflet, 1.3 km radius)
  ✅ Email verification + Google OAuth
  ✅ Admin panel (order management, driver approval, analytics)
  ✅ PWA manifest + service worker

Delivery radius : 1.3 km from kitchen
Delivery fee    : R15 base
Driver payout   : R15 per delivery (credited instantly on completion)
Min withdrawal  : R50 (processed in 24–48 hrs)
Cancellation    : 5 free per month, R20 fee after limit
  — Cancellations ONLY via KotaBot or by calling 065 393 5339

═══════════════════════════════════════════════════════════
                   DELIVERY SCHEDULE (SAST)
═══════════════════════════════════════════════════════════
  Monday – Friday : 09:00 – 17:00
  Saturday        : 09:00 – 14:00
  Sunday          : CLOSED

KotaBot grace window: stays active {POST_CLOSE_GRACE_MINUTES} min after close for tracking questions.
After that, politely tell users we are closed and give next open time.

═══════════════════════════════════════════════════════════
                       MENU
═══════════════════════════════════════════════════════════
{menu_text}

═══════════════════════════════════════════════════════════
                    ORDER STATUSES
═══════════════════════════════════════════════════════════
  pending    → Placed, awaiting payment/confirmation   [CAN cancel]
  paid       → Payment received, kitchen starting      [CAN cancel — R20 fee on next order]
  preparing  → Being cooked right now                  [CANNOT cancel]
  ready      → Ready, driver will be assigned shortly  [CANNOT cancel]
  delivered  → Successfully delivered 🎉               [CANNOT cancel]
  cancelled  → Order was cancelled

═══════════════════════════════════════════════════════════
                   DELIVERY STEPS (driver side)
═══════════════════════════════════════════════════════════
  1. accepted   → Driver accepted the order, heading to restaurant
  2. picked_up  → Driver collected the food
  3. in_transit → Driver on the way to customer
  4. delivered  → Order delivered, driver wallet credited

{order_block}
{history_block}
{driver_block}

═══════════════════════════════════════════════════════════
                  CUSTOMER REWARDS (KotaPoints)
═══════════════════════════════════════════════════════════
  Earning  : 0.1 KotaPoint per R1 spent (delivered orders only)
  Tiers    : Bronze (0–499) → Silver (500–1499) → Gold (1500–2999) → Platinum (3000+)
  Redeem   : 300 kp = R25 off | 650 kp = R50 off | 1500 kp = R120 off
  Codes    : Generated in /rewards, paste at checkout
  NOTE: Only DELIVERED orders count toward points.

KotaPoints calculation you MUST follow:
  1. Sum total_amount of all DELIVERED orders only
  2. Multiply by 0.1
  3. Present as "X kp" (e.g. "34 kp")
  Never show the calculation — only the result.

=======================================================
  Show Order IDs in code format: `ABCD1234` or `full-24-char-id`
=======================================================

═══════════════════════════════════════════════════════════
                  CURRENT CUSTOMER
═══════════════════════════════════════════════════════════
Name  : {user.full_name}
Email : {user.email}
Phone : {phone}

═══════════════════════════════════════════════════════════
                  CANCELLATION RULES
═══════════════════════════════════════════════════════════
- Only cancel when status is "pending" or "paid"
- Ask for confirmation first: "Are you sure you want to cancel order #XXXXXXXX?"
- After customer says YES, embed EXACTLY:
    [CANCEL_ORDER:{{full_24_char_order_id}}]
- ALWAYS use the full 24-character MongoDB ID
- Example: [CANCEL_ORDER:507f1f77bcf86cd799439011]
- Do NOT use the short 8-char code in the tag

═══════════════════════════════════════════════════════════
                    BEHAVIOUR RULES
═══════════════════════════════════════════════════════════
Language & tone:
  - Warm, concise, max 3 short paragraphs per reply
  - Natural kasi slang: sho, lekker, eish, ayt, Ola, ohk, yoh, hayibo 🤯,
    shame, no stress, straight talk, quick-quick, tight, my bad, vibes
  - Add basic SiSwati words naturally (NOT Zulu)
  - Language switch: if user requests it, reply 100% in SiSwati OR 100% in English
  - End warmly: "Lekker day ahead", "Hit me anytime, ayt?", "Sho 🔥"

Time awareness:
  - You know the current SAST time (shown at top of this prompt)
  - If asked "what time is it?" → answer with the exact SAST time above
  - If asked when we open/close → use the schedule above
  - If asked how long ago something happened → calculate from current time
  - Always clarify times are in SAST (UTC+2)

Content rules:
  - NEVER invent prices or menu items not listed above
  - Do NOT take orders — direct to https://foodsorder.vercel.app/menu
  - When user mentions an order ID, find it in history and explain status
  - Confirm feedback: "I've noted it, sho 🙏"
  - If order not in history → ask nicely for full 24-char Order ID
  - For driver questions → refer to Driver Dashboard at https://foodsorder.vercel.app/driver-dashboard
  - For owner/contact → Kgomotso Nkosi · futurekgomotso@gmail.com · 065 393 5339

If delivery is CLOSED and grace period has ended:
  - Do NOT discuss food, prices, or new orders
  - Politely say we are closed and give next opening time
  - Still help with tracking an EXISTING order if they ask
  - Still help drivers with their wallet/stats questions
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "love", "great service", "bad", "slow",
]


def _to_openrouter_messages(messages: List[ChatMessage]) -> List[dict]:
    trimmed = messages[-MAX_HISTORY_TURNS:] if len(messages) > MAX_HISTORY_TURNS else messages
    result = [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in trimmed
    ]
    while result and result[0]["role"] == "assistant":
        result.pop(0)
    deduped: List[dict] = []
    for turn in result:
        if deduped and deduped[-1]["role"] == turn["role"]:
            deduped[-1]["content"] += "\n" + turn["content"]
        else:
            deduped.append(turn)
    return deduped


def _extract_cancel_id(reply: str) -> Optional[str]:
    match = re.search(r"\[CANCEL_ORDER:([0-9a-fA-F]{24})\]", reply)
    return match.group(1) if match else None


async def _execute_cancel(order_id: str, user: User) -> dict:
    try:
        order = await Order.get(order_id)
    except Exception:
        return {"success": False, "reason": "Order not found"}
    if not order:
        return {"success": False, "reason": "Order not found"}
    if order.user_id != str(user.id):
        return {"success": False, "reason": "You can only cancel your own orders"}
    if order.status not in CANCELLABLE_STATUSES:
        status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
        return {"success": False, "reason": f"Cannot cancel — order is already '{status_val}'"}
    order.status = OrderStatus.CANCELLED
    await order.save()
    logger.info(f"Order {order_id} cancelled by {user.email}")
    return {"success": True, "order_id": order_id, "short_id": order_id[-8:].upper()}


async def _maybe_save_suggestion(messages: List[ChatMessage], user: User) -> None:
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if not any(kw in last.lower() for kw in SUGGESTION_KEYWORDS):
        return
    category, sentiment = "general", "neutral"
    if client:
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        "Classify this customer feedback. "
                        "Reply ONLY with valid JSON, no markdown:\n"
                        '{"category":"<food|service|app|general>","sentiment":"<positive|neutral|negative>"}\n'
                        f'Feedback: "{last[:300]}"'
                    ),
                }],
                temperature=0.1, max_tokens=100,
                extra_body={"thinking": {"type": "disabled"}},
            )
            raw = (resp.choices[0].message.content or "").strip("```json").strip("```").strip()
            if raw:
                parsed   = json.loads(raw)
                category = parsed.get("category", "general")
                sentiment = parsed.get("sentiment", "neutral")
        except Exception as e:
            logger.warning(f"Suggestion classification failed: {e}")
    try:
        await Suggestion(
            user_id=str(user.id), user_email=user.email,
            message=last.strip(), category=category, sentiment=sentiment,
            created_at=datetime.utcnow(),
        ).insert()
        logger.info(f"Suggestion saved [{category}/{sentiment}] from {user.email}")
    except Exception as e:
        logger.warning(f"Failed to save suggestion: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    if not client:
        raise HTTPException(503, "AI service not configured — add KIMI_API_KEY to .env")

    # ── Hard lockout check ──
    hours_status = get_status()
    ai_active, lockout_msg = _is_ai_active(hours_status)

    if not ai_active:
        # Even during lockout, still allow order-tracking questions.
        # Check if the last message mentions "track", "order", "status", "where"
        last_msg = ""
        if req.messages:
            last_msg = next((m.content for m in reversed(req.messages) if m.role == "user"), "").lower()
        tracking_keywords = ["track", "order", "status", "where", "driver", "delivered", "when"]
        is_tracking_question = any(kw in last_msg for kw in tracking_keywords)

        if not is_tracking_question:
            now_str = _sast_label()
            return {
                "reply": (
                    f"Eish, KotaBot is resting for now 😴\n\n"
                    f"We closed more than {POST_CLOSE_GRACE_MINUTES} min ago. "
                    f"Current time: **{now_str}**.\n\n"
                    f"{lockout_msg}\n\n"
                    f"Hit me up when we're open again — lekker night! 🌙"
                )
            }

    system_prompt = await build_system_prompt(current_user, req.order_id)
    chat_messages = _to_openrouter_messages(req.messages)

    if not chat_messages:
        return {"reply": "Yebo! How can I help you today?"}

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + chat_messages,
            temperature=0.7,
            max_tokens=4096,
            extra_body={"thinking": {"type": "disabled"}},
        )

        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            reply = "Eish, I couldn't generate a reply right now. Please try again!"

        cancel_id = _extract_cancel_id(reply)
        cancel_result: Optional[dict] = None

        if cancel_id:
            reply = re.sub(r"\[CANCEL_ORDER:[0-9a-fA-F]{24}\]", "", reply).strip()
            cancel_result = await _execute_cancel(cancel_id, current_user)
            logger.info(f"Auto-cancel result for {cancel_id}: {cancel_result}")

        await _maybe_save_suggestion(req.messages, current_user)

        payload: dict = {"reply": reply}
        if cancel_result is not None:
            payload["cancel_result"] = cancel_result
        return payload

    except Exception as e:
        logger.exception("AI chat error")
        raise HTTPException(500, "AI service error. Please try again.")


@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    if not client:
        raise HTTPException(503, "AI service not configured — add KIMI_API_KEY to .env")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    chat_messages = _to_openrouter_messages(req.messages)

    if not chat_messages:
        async def _empty() -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'token': 'Yebo! How can I help you today?'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            stream = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system_prompt}] + chat_messages,
                temperature=0.7, max_tokens=4096,
                extra_body={"thinking": {"type": "disabled"}},
                stream=True,
            )
            async for chunk in stream:
                token = chunk.choices[0].delta.content
                if token:
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception:
            logger.exception("Streaming error")
            yield f"data: {json.dumps({'error': 'AI service error'})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/cancel-order")
async def cancel_order_endpoint(
    body: CancelOrderRequest,
    current_user: User = Depends(get_current_user),
):
    result = await _execute_cancel(body.order_id, current_user)
    if not result["success"]:
        reason = result.get("reason", "")
        if "not found" in reason.lower():       raise HTTPException(404, reason)
        if "only cancel your own" in reason.lower(): raise HTTPException(403, reason)
        raise HTTPException(409, reason)
    logger.info(
        f"Order {body.order_id} cancelled via /cancel-order by {current_user.email}"
        + (f" — reason: {body.reason}" if body.reason else "")
    )
    return {
        "success":   True,
        "message":   "Your order has been cancelled. Sorry to see it go! 🙏",
        "order_id":  body.order_id,
        "short_id":  body.order_id[-8:].upper(),
    }


@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user),
):
    try:
        await Suggestion(
            user_id=str(current_user.id), user_email=current_user.email,
            message=body.message.strip(), category=body.category or "general",
            sentiment="neutral", created_at=datetime.utcnow(),
        ).insert()
        return {"msg": "Thank you! Your feedback has been received."}
    except Exception as e:
        logger.error(f"Suggestion save failed: {e}")
        raise HTTPException(500, "Failed to save feedback")


@router.get("/suggestions")
async def get_suggestions(admin_user: User = Depends(get_current_admin_user)):
    try:
        suggestions = await Suggestion.find_all().to_list(length=500)
        summary = {"positive": 0, "neutral": 0, "negative": 0}
        for s in suggestions:
            key = s.sentiment if s.sentiment in summary else "neutral"
            summary[key] += 1
        return {
            "total":             len(suggestions),
            "sentiment_summary": summary,
            "items": [
                {
                    "id":         str(s.id),
                    "email":      s.user_email,
                    "message":    s.message,
                    "category":   s.category,
                    "sentiment":  s.sentiment,
                    "created_at": s.created_at,
                }
                for s in suggestions
            ],
        }
    except Exception as e:
        logger.error(f"Get suggestions failed: {e}")
        raise HTTPException(500, "Could not load suggestions")


@router.get("/time")
async def get_current_time():
    """Public endpoint — returns current SAST time and delivery status."""
    hours_status = get_status()
    ai_active, _ = _is_ai_active(hours_status)
    return {
        "sast_time":     _sast_label(),
        "is_open":       hours_status["is_open"],
        "ai_active":     ai_active,
        "message":       hours_status["message"],
        "open_time":     hours_status.get("open_time"),
        "close_time":    hours_status.get("close_time"),
        "day":           hours_status.get("day"),
    }


@router.get("/test-ai")
async def test_ai():
    if not client:
        return {"error": "No client — missing KIMI_API_KEY"}
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Say yebo and tell me the current time"}],
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return {"reply": resp.choices[0].message.content, "sast_now": _sast_label()}
    except Exception as e:
        return {"error": str(e)}


@router.get("/debug")
async def debug_openrouter():
    if not client:
        return {"status": "error", "detail": "No client — KIMI_API_KEY missing or empty"}
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Just say 'API works'"}],
            max_tokens=200, temperature=0.0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return {
            "status":   "ok",
            "reply":    (resp.choices[0].message.content or "").strip(),
            "sast_now": _sast_label(),
            "usage":    resp.usage.model_dump() if resp.usage else None,
        }
    except Exception as e:
        return {"status": "error", "detail": str(e), "type": type(e).__name__}
