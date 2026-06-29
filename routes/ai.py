# routes/ai.py
import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from openai import AsyncOpenAI

from dependencies import get_current_user, get_current_admin_user
from services.file_reader_service import ALLOWED_MIME_TYPES, MAX_FILE_BYTES, read_attachment
from services import credits_service
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion
from models.delivery_driver import DeliveryDriver, DriverStatus
from models.delivery_assignment import DeliveryAssignment, AssignmentStatus
from models.wallet_transaction import WalletTransaction
from models.reward_code import RewardCode
from models.notification import AppNotification, NotificationTarget   # ← NEW
from utils.enums import OrderStatus
from utils.business_hours import get_status


router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── OpenRouter Setup ───────────────────────────────────────────────────────
KIMI_API_KEY = os.getenv("KIMI_API_KEY")
MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"

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

# How many minutes after closing KotaBot stays active for tracking questions
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
    return _now_sast().strftime("%A %d %B %Y · %H:%M SAST")


def _is_ai_active(hours_status: dict) -> tuple[bool, str]:
    if hours_status["is_open"]:
        return True, ""

    close_time_str = hours_status.get("close_time")
    if not close_time_str:
        return False, hours_status.get("message", "We are closed today.")

    now = _now_sast()
    ch, cm = map(int, close_time_str.split(":"))
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    minutes_since_close = int((now - close_dt).total_seconds() / 60)

    if minutes_since_close <= POST_CLOSE_GRACE_MINUTES:
        remaining = POST_CLOSE_GRACE_MINUTES - minutes_since_close
        return True, f"[POST-CLOSE GRACE — {remaining} min remaining. Only answer order/tracking/rewards questions, no new orders.]"

    return False, hours_status.get("message", "We are currently closed.")


# ── Rewards context builder ────────────────────────────────────────────────
async def _build_rewards_block(user_id: str) -> tuple[str, int, int, int]:
    """
    Returns (codes_text, earned_kp, redeemed_kp, available_kp).
    Points are computed server-side from delivered orders + claimed codes.
    """
    try:
        delivered = await Order.find({
            "user_id": user_id,
            "status": OrderStatus.DELIVERED.value,
        }).to_list()
        delivered_spend = sum(o.total_amount or 0 for o in delivered)
        earned_kp = round(delivered_spend * 0.1)

        all_codes = await RewardCode.find(RewardCode.user_id == user_id).sort("-created_at").to_list()
        redeemed_kp = sum(c.points_spent for c in all_codes)
        available_kp = max(0, earned_kp - redeemed_kp)

        tier = (
            "Platinum 💎" if earned_kp >= 3000
            else "Gold 🥇"    if earned_kp >= 1500
            else "Silver 🥈"  if earned_kp >= 500
            else "Bronze 🥉"
        )

        if not all_codes:
            codes_text = "  (No codes claimed yet — visit /rewards to claim)"
        else:
            now = datetime.utcnow()
            lines = []
            for c in all_codes[:15]:
                if c.used:
                    state = f"USED on order #{c.applied_order_id[-8:].upper() if c.applied_order_id else '?'}"
                elif now > c.expires_at:
                    state = f"EXPIRED {c.expires_at.strftime('%d %b %Y')}"
                else:
                    days_left = (c.expires_at - now).days
                    state = f"ACTIVE — expires in {days_left}d ({c.expires_at.strftime('%d %b %Y')})"
                lines.append(f"  {c.code}  |  {c.label}  |  R{c.discount:.0f} off  |  {state}")
            codes_text = "\n".join(lines)

        summary = (
            f"Earned: {earned_kp} kp | Redeemed: {redeemed_kp} kp | "
            f"Available: {available_kp} kp | Tier: {tier} | "
            f"Delivered orders counted: {len(delivered)}"
        )
        return summary + "\n\nCodes:\n" + codes_text, earned_kp, redeemed_kp, available_kp

    except Exception as e:
        logger.warning(f"Rewards block failed for {user_id}: {e}")
        return "  (Rewards data unavailable)", 0, 0, 0


# ── Driver context builder ─────────────────────────────────────────────────
async def _build_driver_block(user_id: str) -> str:
    try:
        driver = await DeliveryDriver.find_one(DeliveryDriver.user_id == user_id)
        if not driver:
            return ""

        status_label = {
            "pending":   "Under Review ⏳ — waiting admin approval (up to 24 hrs)",
            "approved":  "Approved ✅ — can go online",
            "active":    "Active Driver ✅",
            "offline":   "Offline — approved but not currently working",
            "rejected":  "Rejected ❌ — contact Kgomotso",
            "suspended": "Suspended ⚠️ — contact Kgomotso",
        }.get(driver.status.value, driver.status.value)

        availability = "🟢 Online — accepting orders" if driver.is_available else "🔴 Offline"

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
                    mins_on_road = f" ({elapsed} min on road)"
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
Vehicle          : {driver.vehicle_type.value.capitalize()}
Reg / License    : {driver.vehicle_registration or 'N/A'} / {driver.drivers_license or 'N/A'}
Status           : {status_label}
Availability     : {availability}
Rating           : {driver.rating:.1f} / 5.0  ({driver.total_ratings} ratings)
Total deliveries : {driver.total_deliveries}

── Wallet ──────────────────────────────────────────────────────
Balance          : R{driver.wallet_balance:.2f}
Total earned     : R{driver.total_earned:.2f}
Total withdrawn  : R{driver.total_withdrawn:.2f}
Banking          : {driver.bank_name or 'Not set'} — {driver.account_number or 'N/A'}
Min withdrawal   : R50.00  (processed in 24–48 hrs)
{tx_lines}
{active_delivery_block}
"""
    except Exception as e:
        logger.warning(f"Driver block build failed for user {user_id}: {e}")
        return ""


# ── NEW: Account status block ──────────────────────────────────────────────
def _build_account_status_block(user: User) -> str:
    """
    Builds a clear summary of the user's moderation status so KotaBot
    can accurately explain restrictions and guide users accordingly.
    """
    now = datetime.utcnow()
    lines: list[str] = []

    if user.is_banned:
        lines.append("⛔  ACCOUNT STATUS : PERMANENTLY BANNED")
        lines.append(f"   Reason          : {user.banned_reason or 'No reason provided'}")
        lines.append(f"   Banned at       : {user.banned_at.strftime('%d %b %Y %H:%M') if user.banned_at else 'Unknown'}")
        lines.append("   Appeal          : Contact futurekgomotso@gmail.com or 065 393 5339")
        lines.append("")
        lines.append("   ⚠️  KOTABOT INSTRUCTION: This account is fully banned.")
        lines.append("   DO NOT assist with ordering, cart, wallet, or rewards.")
        lines.append("   Only provide support contact details and explain the ban.")

    elif user.is_suspended:
        # Check if timed suspension has expired
        if user.suspended_until and now > user.suspended_until:
            lines.append("✅  ACCOUNT STATUS : ACTIVE (suspension period has ended)")
            lines.append("   Note            : The suspension window has passed. User can transact normally.")
        else:
            lines.append("⏸️   ACCOUNT STATUS : SUSPENDED")
            lines.append(f"   Reason          : {user.suspension_reason or 'No reason provided'}")
            lines.append(f"   Suspended at    : {user.suspended_at.strftime('%d %b %Y %H:%M') if user.suspended_at else 'Unknown'}")
            if user.suspended_until:
                remaining_mins = int((user.suspended_until - now).total_seconds() / 60)
                remaining_str = (
                    f"{remaining_mins // 1440}d {(remaining_mins % 1440) // 60}h"
                    if remaining_mins >= 1440
                    else f"{remaining_mins // 60}h {remaining_mins % 60}m"
                    if remaining_mins >= 60
                    else f"{remaining_mins}m"
                )
                lines.append(f"   Lifts at        : {user.suspended_until.strftime('%d %b %Y %H:%M')} ({remaining_str} remaining)")
            else:
                lines.append("   Duration        : Indefinite (admin must lift manually)")
            lines.append("   Appeal          : Contact futurekgomotso@gmail.com or 065 393 5339")
            lines.append("")
            lines.append("   ⚠️  KOTABOT INSTRUCTION: Account is suspended.")
            lines.append("   DO NOT allow new orders, cart, wallet, or rewards.")
            lines.append("   CAN still: explain the suspension, give support contacts, answer tracking questions.")

    elif user.warning_count >= 3:
        lines.append("🔒  ACCOUNT STATUS : RESTRICTED (3+ warnings)")
        lines.append(f"   Warning count   : {user.warning_count}")
        lines.append("   Restrictions    : Cannot add to cart, checkout, order, use wallet, or redeem rewards.")
        lines.append("   Can still       : View orders, use KotaBot chat.")
        lines.append("")
        lines.append("   ⚠️  KOTABOT INSTRUCTION: Do NOT help with ordering or payments.")
        lines.append("   Politely explain they have reached the warning limit and must contact support.")

    elif user.warning_count > 0:
        last_warning = user.warnings[-1] if user.warnings else None
        lines.append(f"⚠️   ACCOUNT STATUS : WARNED ({user.warning_count} warning(s))")
        if last_warning:
            lines.append(f"   Latest warning  : {last_warning.reason}")
            lines.append(f"   Issued at       : {last_warning.issued_at.strftime('%d %b %Y %H:%M')}")
            lines.append(f"   Issued by       : {last_warning.issued_by_name}")
        lines.append("   Note            : Account is fully functional. 3 warnings = restricted.")

    else:
        lines.append("✅  ACCOUNT STATUS : ACTIVE — no restrictions")

    block = "\n".join(lines)

    # List all warnings in full for context
    if user.warnings:
        warning_lines = []
        for idx, w in enumerate(user.warnings, 1):
            warning_lines.append(
                f"  {idx}. [{w.issued_at.strftime('%d %b %Y')}] {w.reason}"
                + (f" — \"{w.message}\"" if w.message else "")
                + f" (by {w.issued_by_name})"
            )
        block += "\n\nAll warnings on record:\n" + "\n".join(warning_lines)

    return block


# ── NEW: Notifications block ───────────────────────────────────────────────
async def _build_notifications_block(user_id: str) -> str:
    """
    Fetches active, non-expired admin notifications targeting this user
    (broadcast + specific). Returns a formatted block for the system prompt
    so KotaBot can proactively surface important messages.
    """
    try:
        now = datetime.utcnow()
        notifications = await AppNotification.find({
            "is_active": True,
            "expires_at": {"$gt": now},
            "$or": [
                {"target": NotificationTarget.ALL.value},
                {"target": NotificationTarget.SPECIFIC.value, "target_user_id": user_id},
            ],
        }).sort("-created_at").limit(20).to_list()

        if not notifications:
            return "  (No active notifications)"

        TYPE_ICONS = {
            "info":        "ℹ️ ",
            "warning":     "⚠️ ",
            "maintenance": "🔧",
            "promo":       "🎁",
            "update":      "🆕",
            "urgent":      "🚨",
        }

        lines = []
        unread_ids = [n for n in notifications if user_id not in n.read_by]

        for n in notifications:
            icon       = TYPE_ICONS.get(n.type.value, "📣")
            read_label = "UNREAD" if user_id not in n.read_by else "read"
            target_label = "→ YOU specifically" if n.target.value == "specific" else "broadcast"
            days_left  = max(0, (n.expires_at - now).days)
            lines.append(
                f"  {icon} [{n.type.value.upper()}] [{read_label}] [{target_label}]  "
                f"expires in {days_left}d\n"
                f"     Title  : {n.title}\n"
                f"     Message: {n.message}\n"
                f"     From   : {n.created_by_name}  |  Sent: {n.created_at.strftime('%d %b %Y %H:%M')}"
            )

        unread_count = len(unread_ids)
        header = (
            f"Active notifications: {len(notifications)} total, "
            f"{unread_count} unread"
        )

        # KotaBot instruction for urgent/unread messages
        urgent_unread = [
            n for n in unread_ids
            if n.type.value in ("urgent", "warning", "maintenance")
        ]
        instruction = ""
        if urgent_unread:
            titles = ", ".join(f'"{n.title}"' for n in urgent_unread[:3])
            instruction = (
                f"\n   ⚠️  KOTABOT INSTRUCTION: There are {len(urgent_unread)} urgent/unread "
                f"notification(s) for this user ({titles}). "
                "Proactively mention them at the start of your reply "
                "or when relevant to the conversation."
            )

        return header + instruction + "\n\n" + "\n\n".join(lines)

    except Exception as e:
        logger.warning(f"Notifications block failed for {user_id}: {e}")
        return "  (Notifications data unavailable)"


# ── System Prompt ──────────────────────────────────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:

    hours_status = get_status()
    current_time = _sast_label()
    ai_active, ai_status_note = _is_ai_active(hours_status)

    hours_block = (
        f"DELIVERY: OPEN ✅ — closes at {hours_status['close_time']} SAST today ({hours_status['day']})"
        if hours_status["is_open"]
        else f"DELIVERY: CLOSED 🔴 — {hours_status['message']}"
    )

    # ── Menu ──────────────────────────────────────────────────────────────
    try:
        items = await MenuItem.find_all().to_list(length=60)
        menu_text = "\n".join(
            f"  • {i.name:<30} R{i.price:>6.2f}  [{i.category}]"
            + (f"\n    {i.description[:100]}" if i.description else "")
            for i in items
        ) or "  (Menu currently empty)"
    except Exception:
        menu_text = "  (Menu unavailable)"

    # ── Active order ──────────────────────────────────────────────────────
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str  = ", ".join(f"{it.name} ×{it.quantity}" for it in (order.items or []))
                status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
                can_cancel = order.status in CANCELLABLE_STATUSES

                driver_assignment = None
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

                discount_line = f"\nDiscount    : -R{order.discount:.2f} (reward code applied)" if order.discount else ""

                order_block = f"""
╔═══════════════════════════════════════════════════╗
║                  ACTIVE ORDER                      ║
╚═══════════════════════════════════════════════════╝
Order #{str(order.id)[-8:].upper()} (full ID: {str(order.id)})
Status      : {status_val.upper()}
Total       : R{order.total_amount:.2f}
Delivery fee: R{order.delivery_fee or 0:.2f}{discount_line}
Items       : {items_str or 'none'}
Payment     : {order.payment_method or 'paystack'}
Address     : {order.delivery_address or 'Not specified'}
Phone       : {order.phone or 'Not provided'}
Placed at   : {order.created_at.strftime('%d %b %Y %H:%M SAST')}
Cancellable : {'YES (still ' + status_val + ')' if can_cancel else 'NO (already ' + status_val + ')'}
{driver_info_line}
"""
        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    # ── Order history ─────────────────────────────────────────────────────
    history_block = ""
    try:
        recent = await Order.find(Order.user_id == str(user.id)).to_list(length=20)
        if recent:
            recent.sort(key=lambda o: o.created_at, reverse=True)
            total_spent     = sum(o.total_amount for o in recent)
            delivered_total = sum(
                o.total_amount for o in recent
                if (o.status.value if hasattr(o.status, "value") else str(o.status)) == "delivered"
            )
            lines = []
            for o in recent:
                status     = o.status.value if hasattr(o.status, "value") else str(o.status)
                items_str  = ", ".join(f"{it.name} ×{it.quantity}" for it in (o.items or []))
                can_cancel = o.status in CANCELLABLE_STATUSES
                disc_str   = f" | disc R{o.discount:.2f}" if o.discount else ""
                lines.append(
                    f"  #{str(o.id)[-8:].upper()} (ID:{str(o.id)}) | "
                    f"{status:10} | R{o.total_amount:>7.2f}{disc_str} | "
                    f"{o.payment_method or 'paystack':8} | "
                    f"{o.created_at.strftime('%d %b %Y %H:%M')} | "
                    f"{items_str} | {'can cancel' if can_cancel else 'locked'}"
                )
            history_block = (
                f"=== ORDER HISTORY ({len(recent)} orders | "
                f"R{total_spent:.2f} total spent | R{delivered_total:.2f} delivered) ===\n"
                + "\n".join(lines)
            )
        else:
            history_block = "=== ORDER HISTORY ===\nNo previous orders yet."
    except Exception as e:
        logger.warning(f"Order history fetch failed: {e}")

    # ── Rewards ──────────────────────────────────────────────────────────
    rewards_text, earned_kp, redeemed_kp, available_kp = await _build_rewards_block(str(user.id))

    # ── Driver block ──────────────────────────────────────────────────────
    driver_block = await _build_driver_block(str(user.id))

    # ── Account status block (NEW) ────────────────────────────────────────
    account_status_block = _build_account_status_block(user)

    # ── Notifications block (NEW) ─────────────────────────────────────────
    notifications_block = await _build_notifications_block(str(user.id))

    phone   = getattr(user, "phone", None) or "Not on file"
    is_admin = getattr(user, "is_admin", False)
    auth_method = "Social login (Google/GitHub/Spotify)" if not getattr(user, "hashed_password", None) else "Email + password"
    verified = "✅ Verified" if user.email_verified else "⚠️  NOT verified — prompt them to check inbox"

    # ── AI availability note ──────────────────────────────────────────────
    if not ai_active:
        ai_availability_block = f"""
╔══════════════════════════════════════════════════════╗
║  ⛔  KOTABOT IS NOW IN SILENT MODE                    ║
║  Delivery closed more than {POST_CLOSE_GRACE_MINUTES} minutes ago.           ║
║  DO NOT answer new food/order questions.             ║
║  You MAY still: greet, explain when we reopen, help  ║
║  with existing order tracking & rewards ONLY.        ║
╚══════════════════════════════════════════════════════╝
{ai_status_note}
"""
    elif ai_status_note:
        ai_availability_block = f"\n[NOTE: {ai_status_note}]\n"
    else:
        ai_availability_block = ""

    return f"""You are KotaBot 🤖 — the friendly, street-smart AI assistant for KotaBites, Johannesburg's favourite kota delivery service. You know this app inside-out.

══════════════════════════════════════════════════════════════════
  🕐 CURRENT TIME (SAST) : {current_time}
  📍 LOCATION            : Tjovitjo Phase 2, Johannesburg South, SA
  {hours_block}
══════════════════════════════════════════════════════════════════
{ai_availability_block}

════════════════════════════════════════════════════════════════════════
                          ABOUT KOTABITES
════════════════════════════════════════════════════════════════════════
KotaBites is a kota sandwich delivery platform serving Johannesburg South.
Everything is ordered online — fresh kotas delivered within 1.3 km radius.

Owner / Founder is Male : name -> Kgomotso surname -> Nkosi, gender->(him/he/Mr.) Male 
Email            : futurekgomotso@gmail.com
Phone            : 065 393 5339  (also for urgent cancellations)
Website          : https://foodsorder.vercel.app
API Docs         : https://kotabites.onrender.com/docs
WhatsApp         : https://wa.me/27634414863

── Tech Stack (Don't expose database storing) ──────────────────────────────────────────────────────────
Frontend   : React 19 + Vite + TailwindCSS   → Vercel
Backend    : FastAPI + MongoDB (Beanie ODM)  → Render (free tier — cold starts ~60s)
Payments   : Paystack (card, EFT, Instant EFT)
Images     : Cloudinary
AI         : OpenRouter → nvidia/nemotron-3-super (that's you!)
Email      : EmailJS (client-side, no SMTP)
Auth       : JWT + Google + GitHub + Spotify OAuth
Maps       : React Leaflet  (delivery coverage checker at /coverage)
Video call : ZegoCloud  (driver ↔ customer voice/video on order tracker)
Fonts      : Bebas Neue (headings) + Plus Jakarta Sans (body)
State      : React Context (Auth, Cart, Order) + Zustand
HTTP       : axios + axiosClient interceptor (Bearer token from sessionStorage "kb_token")

════════════════════════════════════════════════════════════════════════
                       DELIVERY SCHEDULE (SAST)
════════════════════════════════════════════════════════════════════════
  Monday – Friday : 09:00 – 17:00
  Saturday        : 09:00 – 14:00
  Sunday          : CLOSED

KotaBot stays active {POST_CLOSE_GRACE_MINUTES} min after close for tracking/rewards questions.

════════════════════════════════════════════════════════════════════════
                             PRICING
════════════════════════════════════════════════════════════════════════
Delivery fee tiers (dynamic, based on subtotal before discount):
  R0 – R50    →  R8  delivery fee
  R50 – R100  →  R12 delivery fee
  R100+       →  R15 delivery fee

Payment limits:
  Cash on Delivery  : maximum order total R150
  Paystack (online) : maximum order total R250
  (for larger orders call Kgomotso at 065 393 5339)

Cancellation policy:
  - 5 FREE cancellations per calendar month
  - R20 fee charged on the NEXT order after the limit is exhausted
  - Only cancellable when status = pending or paid
  - Cancellations ONLY via KotaBot or by calling 065 393 5339

Driver payout : R15 per delivery (wallet credited instantly on completion)
Min withdrawal: R50  |  Processing: 24–48 hrs to bank account

════════════════════════════════════════════════════════════════════════
                         ALL APP PAGES & FEATURES
════════════════════════════════════════════════════════════════════════
/               Home — hero + order tracker widget + delivery coverage map
/menu           Browse menu, add to cart, 3D rotating card viewer, search + filter
/cart           Cart: adjust quantities, view subtotal, proceed to checkout
/checkout       Delivery details, payment method, promo/reward code field, order summary
/rewards        Customer rewards wallet: KotaPoints balance, tiers, claim codes, history
/order/:id      Live order tracker: status stepper, driver info, ZegoCloud call buttons (polls 5s)
/wallet         Driver earnings wallet: balance, transactions, withdrawal modal
/driver-dashboard  Driver hub: profile, go online/offline, available orders, delivery steps
/deliver        Driver application form (ID, vehicle, banking, document uploads)
/coverage       Leaflet map — 1.3 km delivery radius checker (enter address or use GPS)
/login          Email/password + Google + GitHub + Spotify OAuth
/register       Create account (email verification sent via EmailJS)
/verify-email   Email verification flow (token in URL)
/forgot-password / /reset-password   Password reset via EmailJS
/info           Policies: cancellation (5 free/R20), refunds, T&Cs, support

Key UI interactions:
  ✅ 3D card viewer on menu (drag-to-orbit, zoom, depth faces)
  ✅ Active order tracker banner on /menu (polls 8s, shows driver steps + call buttons)
  ✅ ZegoCloud voice AND video call between driver & customer (room = order ID)
  ✅ KotaBot AI chat widget — bottom-right FAB, always mounted across all pages
  ✅ Toast notification system (cart/success/error/info, 5s auto-dismiss)
  ✅ Business hours gate on checkout — shows schedule if closed
  ✅ Cold-start notice (Render free tier sleeps — 30–60s wake time)
  ✅ PWA (installable, manifest.json, service worker sw.js)
  ✅ Google Ads account linked (ca-pub-2722864790738174)
  ✅ Google site verification (Search Console)
  ✅ Microsoft Clarity analytics
  ✅ SEO: OpenGraph, Twitter card, schema.org Restaurant JSON-LD

════════════════════════════════════════════════════════════════════════
                           MENU
════════════════════════════════════════════════════════════════════════
{menu_text}

════════════════════════════════════════════════════════════════════════
                        ORDER STATUSES
════════════════════════════════════════════════════════════════════════
  pending    → Placed, awaiting payment confirmation       [CAN cancel — free]
  paid       → Payment received, kitchen notified          [CAN cancel — R20 fee on next order]
  preparing  → Kitchen is cooking right now                [CANNOT cancel]
  ready      → Done, driver being assigned                 [CANNOT cancel]
  delivered  → Successfully delivered 🎉                   [CANNOT cancel]
  cancelled  → Order was cancelled

════════════════════════════════════════════════════════════════════════
                    DELIVERY STEPS (driver side)
════════════════════════════════════════════════════════════════════════
  1. accepted   → Driver accepted, heading to restaurant
  2. picked_up  → Driver collected the food from kitchen
  3. in_transit → Driver on the way to customer's address
  4. delivered  → Delivered ✅  — R15 credited to driver wallet instantly

════════════════════════════════════════════════════════════════════════
                  CUSTOMER REWARDS — KotaPoints
════════════════════════════════════════════════════════════════════════
Earning rule   : R1 spent on a DELIVERED order = 0.1 KotaPoint
                 (ONLY delivered orders count — pending/cancelled/preparing = 0)

Tiers (based on ALL-TIME earned points):
  Bronze   0   – 499  pts  🥉  (default)
  Silver   500 – 1 499 pts  🥈
  Gold     1500– 2 999 pts  🥇
  Platinum 3000+       pts  💎  (maximum — VIP status)

Redeem at /rewards wallet:
  300  kp  →  R25 off    (code valid 30 days, single-use)
  650  kp  →  R50 off
  1500 kp  →  R120 off

Discount mechanics at checkout:
  - Discount first applied to food subtotal
  - If discount > subtotal, the excess reduces the delivery fee
  - Delivery fee cannot go below R0 (free delivery)
  - Code format: KB + 22 alphanumeric chars, e.g. KBXR9Q2A4F...

KotaPoints calculation YOU MUST ALWAYS follow:
  1. Look at order history above — sum total_amount of DELIVERED orders ONLY
  2. Multiply by 0.1 → round to nearest integer = earned_kp
  3. Subtract points_spent from ALL claimed codes = redeemed_kp
  4. available_kp = max(0, earned_kp - redeemed_kp)
  ⚠️ Never show the formula — only present the result, e.g. "47 kp available"

THIS CUSTOMER'S REWARDS SNAPSHOT:
{rewards_text}

════════════════════════════════════════════════════════════════════════
                         DRIVER SYSTEM
════════════════════════════════════════════════════════════════════════
Driver statuses:
  pending   → Application submitted, admin reviews within 24 hrs
  approved  → Approved ✅ — can toggle online in Driver Dashboard
  active    → Currently online & accepting orders
  offline   → Approved but not working right now
  rejected  → Application rejected — contact Kgomotso (065 393 5339)
  suspended → Account suspended — contact Kgomotso

Onboarding steps:
  1. Fill /deliver form: full name, phone, SA ID number, vehicle type,
     vehicle registration, driver's licence, street address, suburb,
     postal code, banking details (bank + account number + holder)
  2. Upload documents: ID photo, licence, vehicle doc, profile photo (max 5 MB each, images only)
  3. Admin approves via admin panel → status → approved
  4. Driver goes to /driver-dashboard → toggle online
  5. Admin marks order "Ready" → appears in Driver Dashboard → Orders tab
  6. Driver accepts → picks up → marks in transit → delivers
  7. R15 added to wallet; driver can withdraw min R50 to bank

Vehicle types: bicycle, motorcycle, car, scooter
Banking options: FNB, Standard Bank, Capitec, Nedbank, ABSA
Delivery coverage: 1.3 km from kitchen (Tjovitjo Phase 2, Joburg South)

{driver_block}

════════════════════════════════════════════════════════════════════════
                       CURRENT CUSTOMER
════════════════════════════════════════════════════════════════════════
Name       : {user.full_name}
Email      : {user.email}
Phone      : {phone}
Auth method: {auth_method}
Email      : {verified}
Admin      : {'✅ YES — has admin panel access' if is_admin else 'No'}

{order_block}
{history_block}

════════════════════════════════════════════════════════════════════════
                     ACCOUNT MODERATION STATUS
════════════════════════════════════════════════════════════════════════
{account_status_block}

════════════════════════════════════════════════════════════════════════
                     ADMIN NOTIFICATIONS FOR THIS USER
════════════════════════════════════════════════════════════════════════
{notifications_block}

════════════════════════════════════════════════════════════════════════
                      CANCELLATION RULES
════════════════════════════════════════════════════════════════════════
- Only cancel if status is "pending" or "paid"
- ALWAYS confirm first: "Are you sure you want to cancel order #XXXXXXXX?"
- After customer says YES, embed EXACTLY this tag in your reply:
    [CANCEL_ORDER:{{full_24_char_order_id}}]
- ALWAYS use the full 24-character OrderId (OR the short 8-char code)
- Example: [CANCEL_ORDER:507f1f77bcf86cd799439011]
- If the order is preparing/ready/delivered → explain clearly it cannot be cancelled

════════════════════════════════════════════════════════════════════════
                       BEHAVIOUR RULES
════════════════════════════════════════════════════════════════════════
Language & tone:
  - Warm, helpful, concise — max 3 short paragraphs per reply
  - Natural kasi slang: sho, lekker, eish, ayt, yoh, hayibo 🤯, shame,
    no stress, straight talk, quick-quick, tight, my bad, vibes, sharp
  - Sprinkle basic SiSwati naturally (NOT Zulu)
  - Language switch: if user requests it, reply 100% in SiSwati OR 100% in English only
  - Sign off warmly: "have a good day ahead 🔥", "Hit me anytime, ayt?", "Sho 🙏", "Stay sharp 🧡"

Time awareness:
  - You know the EXACT current SAST time shown at the top of this prompt
  - Answer "what time is it?" with the exact time from above
  - Calculate time differences precisely ("that order was placed 2 hrs ago")
  - Always clarify times are SAST (UTC+2, no daylight saving)

Helpful links to share when relevant:
  Menu            : https://foodsorder.vercel.app/menu
  Rewards wallet  : https://foodsorder.vercel.app/rewards
  Order tracker   : https://foodsorder.vercel.app/order/<id>
  Driver dashboard: https://foodsorder.vercel.app/driver-dashboard
  Coverage map    : https://foodsorder.vercel.app/coverage
  Policies        : https://foodsorder.vercel.app/info
  Driver signup   : https://foodsorder.vercel.app/deliver
  Support phone   : 065 393 5339  (Kgomotso Nkosi)
  Support email   : futurekgomotso@gmail.com
  WhatsApp        : https://wa.me/27634414863

Content rules:
  - NEVER invent menu items or prices not in the menu above
  - Do NOT place orders for the user — always link to /menu
  - When user mentions an order ID → find it in history and explain status clearly
  - If order not found in history → ask for the full 24-char MongoDB Order ID
  - When asked about KotaPoints → compute from the history above using the formula
  - When asked about a reward code → check the codes section above
  - For checkout promo issues → explain they need an ACTIVE (unused, non-expired) code from /rewards
  - For driver questions → refer to /driver-dashboard
  - For delivery area → refer to /coverage (1.3 km from Tjovitjo Phase 2)
  - For password reset → refer to /forgot-password
  - For email verification → ask them to check inbox or go to /verify-email
  - For billing/payment → Paystack handles it; reference = payment_reference on order
  - If server feels slow → mention it's on Render free tier, cold starts ~30–60s, normal
  - If the user is ADMIN (is_admin = True) → they can manage orders/drivers at the admin panel

Account moderation rules (CRITICAL — enforce strictly):
  - If account is BANNED → refuse all transactional help; direct to support only
  - If account is SUSPENDED (and window has NOT passed) → refuse ordering/wallet/rewards; still track orders
  - If account is RESTRICTED (3+ warnings) → refuse ordering/wallet/rewards; explain clearly
  - If account is WARNED (1-2 warnings) → allow all features; gently remind them about warnings if relevant
  - NEVER pretend restrictions don't exist; be honest but empathetic about moderation status
  - Always point to futurekgomotso@gmail.com or 065 393 5339 for moderation appeals

Notification rules:
  - If there are UNREAD notifications → surface them naturally in conversation
  - URGENT/WARNING type unread → mention proactively at the top of your first reply
  - PROMO type → mention when relevant (user asks about deals, rewards, menu)
  - MAINTENANCE type → warn user if they're about to take an action that might be affected
  - INFO/UPDATE type → mention when directly relevant to the topic
  - Don't dump all notifications at once — weave them in naturally
  - After mentioning a notification, you can say "You can dismiss this in the notifications bell 🔔"

Rewards help (detailed):
  - If customer asks "how many points do I have?" → calculate from delivered orders above
  - Explain which orders earned points and which didn't (only delivered count)
  - Show their current tier and how many more points to reach next tier
  - For active codes → show from the codes section, remind them to paste at checkout
  - Expired codes → empathise, remind they had 30 days, encourage to claim again if enough kp

Driver help (detailed):
  - If this user is also a driver → address both roles naturally
  - Pending → "Hang tight, admin usually approves within 24 hrs sho"
  - Rejected/Suspended → direct to Kgomotso, don't speculate on reason
  - Wallet withdrawal: must have R50+ balance, banking details set in profile, 24–48 hr processing
  - Going online: Driver Dashboard → toggle at the top → green = online
  - No orders showing: orders only appear when admin marks them "Ready"; auto-refreshes every 15s
  - ZegoCloud calls: room ID = "kotabites-order-<last 8 chars of order ID>"

When delivery is CLOSED and grace period ended:
  - Do NOT discuss food, prices, or new orders
  - Politely give next opening time from the schedule above
  - Still help with existing order tracking and rewards questions
  - Still help drivers with wallet/stats questions

Always show Order IDs in code format: `ABCD1234` (short) or full 24-char ID when needed for cancellation.

Use svg to make unique icons 
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
                parsed    = json.loads(raw)
                category  = parsed.get("category", "general")
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

    hours_status = get_status()
    ai_active, lockout_msg = _is_ai_active(hours_status)

    if not ai_active:
        last_msg = ""
        if req.messages:
            last_msg = next((m.content for m in reversed(req.messages) if m.role == "user"), "").lower()
        tracking_keywords = [
            "track", "order", "status", "where", "driver", "delivered",
            "when", "reward", "points", "wallet", "kota", "code",
        ]
        is_tracking_question = any(kw in last_msg for kw in tracking_keywords)

        if not is_tracking_question:
            return {
                "reply": (
                    f"Eish, KotaBot is resting for now 😴\n\n"
                    f"We closed more than {POST_CLOSE_GRACE_MINUTES} min ago. "
                    f"Current time: **{_sast_label()}**.\n\n"
                    f"{lockout_msg}\n\n"
                    f"Hit me up when we're open again — lekker night! 🌙"
                )
            }

    system_prompt = await build_system_prompt(current_user, req.order_id)
    chat_messages = _to_openrouter_messages(req.messages)

    if not chat_messages:
        return {"reply": "Yebo! How can I help you today?"}

    # FREE-plan credit gate — no-op for PROBITE. Placed here (not earlier)
    # so canned replies above (closed hours, empty message) never cost a
    # credit — only an actual model call does.
    await credits_service.require_credits(current_user)

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

        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        credits_charged = await credits_service.charge_for_tokens(current_user, total_tokens)

        payload: dict = {"reply": reply}
        if cancel_result is not None:
            payload["cancel_result"] = cancel_result
        payload["credits"] = {
            "charged": credits_charged,
            **(await credits_service.get_status(current_user)),
        }
        return payload

    except Exception:
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

    # FREE-plan credit gate — same as /chat, checked before streaming starts
    # so a blocked user never opens a connection that goes nowhere.
    await credits_service.require_credits(current_user)

    async def event_generator() -> AsyncGenerator[str, None]:
        total_tokens: Optional[int] = None
        reply_chars = 0
        try:
            stream = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system_prompt}] + chat_messages,
                temperature=0.7, max_tokens=4096,
                extra_body={
                    "thinking": {"type": "disabled"},
                    "stream_options": {"include_usage": True},
                },
                stream=True,
            )
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage and getattr(usage, "total_tokens", None):
                    total_tokens = usage.total_tokens

                if not chunk.choices:
                    continue  # final usage-only chunk has no choices
                token = chunk.choices[0].delta.content
                if token:
                    reply_chars += len(token)
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception:
            logger.exception("Streaming error")
            yield f"data: {json.dumps({'error': 'AI service error'})}\n\n"
        finally:
            # Charge even if the client disconnects mid-stream — the model
            # call already happened. If OpenRouter didn't send a usage
            # chunk, fall back to a rough chars→tokens estimate.
            estimated_tokens = total_tokens or (reply_chars // 4 if reply_chars else None)
            credits_charged = await credits_service.charge_for_tokens(current_user, estimated_tokens)
            status = await credits_service.get_status(current_user)
            yield f"data: {json.dumps({'credits': {'charged': credits_charged, **status}})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/chat/read-file")
async def ai_chat_read_file(
    file: UploadFile = File(...),
    question: Optional[str] = Form(None),
    chained: bool = Query(
        False,
        description=(
            "True when the frontend is about to immediately fold this result "
            "into a /chat call (file-attach-then-send). False for a standalone "
            "read (e.g. voice-note transcription dropped into the input box). "
            "Chained reads aren't charged here — the longer prompt they "
            "produce already raises the cost of the /chat call that follows; "
            "charging both would bill the same attachment twice."
        ),
    ),
    current_user: User = Depends(get_current_user),
):
    """
    Lets a customer attach a file (image, PDF, or voice note) to their
    KotaBot chat.

    This is a pure file → text step — it does NOT touch chat history or
    call OpenRouter. For images/PDFs, the frontend folds the returned
    `description` into the next /chat (or /chat/stream) message as hidden
    context, e.g.:

        "[Attached file: receipt.jpg]\\n<description>\\n\\nMy question: ..."

    For voice notes (audio/*), `description` is a verbatim transcript
    instead — the frontend drops it straight into the chat input for the
    customer to review/edit before sending, so no folding needed there.

    `question` is optional — pass the customer's accompanying message text
    (e.g. "is this payment valid?") so Gemini's read stays relevant to
    what they actually asked. Leave blank for voice notes.
    """
    # FREE-plan credit gate. Still enforced even when chained=True — a user
    # at 0 credits shouldn't get a free Gemini read just because /chat will
    # reject them a moment later anyway.
    await credits_service.require_credits(current_user)

    contents = await file.read()

    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(413, f"File too large — max {MAX_FILE_BYTES // (1024 * 1024)} MB")

    # Normalize away codec params browsers append, e.g. browser MediaRecorder
    # often reports "audio/webm;codecs=opus" rather than the bare mime type.
    mime_type = (file.content_type or "").split(";")[0].strip().lower()
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            415,
            f"'{mime_type or 'unknown'}' isn't supported — upload an image, PDF, or voice note.",
        )

    result = await read_attachment(contents, mime_type, question or "")

    if not result.ok:
        raise HTTPException(422, result.reason or "Couldn't read that file.")

    await credits_service.charge(
        current_user, 0 if chained else credits_service.COST_FILE_READ
    )

    logger.info(
        f"[file_reader] read {file.filename!r} ({mime_type}, {len(contents)} bytes) "
        f"for {current_user.email}{' [chained]' if chained else ''}"
    )

    return {
        "filename": file.filename,
        "mime_type": mime_type,
        "description": result.description,
        "credits": {
            "charged": 0 if chained else credits_service.COST_FILE_READ,
            **(await credits_service.get_status(current_user)),
        },
    }


@router.post("/cancel-order")
async def cancel_order_endpoint(
    body: CancelOrderRequest,
    current_user: User = Depends(get_current_user),
):
    result = await _execute_cancel(body.order_id, current_user)
    if not result["success"]:
        reason = result.get("reason", "")
        if "not found" in reason.lower():
            raise HTTPException(404, reason)
        if "only cancel your own" in reason.lower():
            raise HTTPException(403, reason)
        raise HTTPException(409, reason)
    logger.info(
        f"Order {body.order_id} cancelled via /cancel-order by {current_user.email}"
        + (f" — reason: {body.reason}" if body.reason else "")
    )
    return {
        "success":  True,
        "message":  "Your order has been cancelled. Sorry to see it go! 🙏",
        "order_id": body.order_id,
        "short_id": body.order_id[-8:].upper(),
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
        "sast_time":  _sast_label(),
        "is_open":    hours_status["is_open"],
        "ai_active":  ai_active,
        "message":    hours_status["message"],
        "open_time":  hours_status.get("open_time"),
        "close_time": hours_status.get("close_time"),
        "day":        hours_status.get("day"),
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
