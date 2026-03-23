# routes/ai.py
import os
import re
import json
import logging
from datetime import datetime
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


# ── Driver context builder ─────────────────────────────────────────────────
async def _build_driver_block(user_id: str) -> str:
    """
    Look up whether this user is also a driver.
    Returns a formatted block for the system prompt, or empty string if not a driver.
    """
    try:
        driver = await DeliveryDriver.find_one(DeliveryDriver.user_id == user_id)
        if not driver:
            return ""

        status_label = {
            "pending":   "Under Review",
            "approved":  "Active Driver",
            "rejected":  "Rejected",
            "suspended": "Suspended",
        }.get(driver.status.value, driver.status.value)

        availability = "Online (accepting orders)" if driver.is_available else "Offline"

        recent_tx = await WalletTransaction.find(
            WalletTransaction.driver_id == str(driver.id)
        ).sort("-created_at").limit(5).to_list()

        tx_lines = ""
        if recent_tx:
            tx_lines = "\nRecent transactions:\n" + "\n".join(
                f"  - {t.type.value:20} | {'+'if t.amount>0 else ''}R{t.amount:.2f} "
                f"| Bal after: R{t.balance_after:.2f} | {t.created_at.strftime('%d %b %Y')}"
                for t in recent_tx
            )

        active_delivery_line = ""
        if driver.current_order_id:
            active_delivery_line = f"\nCurrently delivering order: #{driver.current_order_id[-8:].upper()}"

        return f"""
=== THIS USER IS ALSO A DRIVER ===
Name          : {driver.full_name}
Status        : {status_label}
Availability  : {availability}
Vehicle       : {driver.vehicle_type.value.capitalize()}
Rating        : {driver.rating:.1f} / 5.0  ({driver.total_ratings} ratings)
Total deliveries: {driver.total_deliveries}
Wallet balance  : R{driver.wallet_balance:.2f}
Total earned    : R{driver.total_earned:.2f}
Total withdrawn : R{driver.total_withdrawn:.2f}{active_delivery_line}{tx_lines}

DRIVER BEHAVIOUR RULES:
- Address them as a driver when relevant, e.g. "As a driver, your balance is..."
- Help them understand their wallet, earnings, and delivery stats
- If they ask about going online/offline, explain they can do that in the Driver Dashboard
- If status is "pending", remind them approval takes up to 24 hours
- If status is "rejected" or "suspended", direct them to contact support
- If they ask about withdrawals, minimum is R50 and processing takes 24–48 hours
- You can reference their exact balance and delivery count from above
- Still help them with customer questions too — drivers order food as well
"""
    except Exception as e:
        logger.warning(f"Driver block build failed for user {user_id}: {e}")
        return ""


# ── System Prompt ──────────────────────────────────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:

    hours_status = get_status()
    if hours_status["is_open"]:
        hours_block = (
            f"DELIVERY STATUS: OPEN — closes at {hours_status['close_time']} SAST today ({hours_status['day']})"
        )
    else:
        hours_block = (
            f"DELIVERY STATUS: CLOSED — {hours_status['message']}"
        )

    try:
        items = await MenuItem.find_all().to_list(length=60)
        menu_text = "\n".join(
            f"- {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description[:100]}" if i.description else "")
            for i in items
        ) or "(Menu currently empty)"
    except Exception:
        menu_text = "(Menu unavailable)"

    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(
                    f"{it.name} x{it.quantity}" for it in (order.items or [])
                )
                status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
                can_cancel = order.status in CANCELLABLE_STATUSES
                order_block = f"""
=== ACTIVE ORDER ===
Order #{str(order.id)[-8:].upper()} (full ID: {str(order.id)}) — Status: {status_val.upper()}
Total   : R{order.total_amount:.2f}
Items   : {items_str or "none"}
Payment : {order.payment_method or "paystack"}
Address : {order.delivery_address or "Not specified"}
Phone   : {order.phone or "Not provided"}
Cancellable: {"YES (status is still " + status_val + ")" if can_cancel else "NO (already " + status_val + ")"}
"""
        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    history_block = ""
    try:
        recent = await Order.find(Order.user_id == str(user.id)).to_list(length=10)
        if recent:
            recent.sort(key=lambda o: o.created_at, reverse=True)
            total_spent = sum(o.total_amount for o in recent)
            lines = []
            for o in recent:
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                items_str = ", ".join(f"{it.name} x{it.quantity}" for it in (o.items or []))
                can_cancel = o.status in CANCELLABLE_STATUSES
                lines.append(
                    f"  - #{str(o.id)[-8:].upper()} (ID:{str(o.id)}) | {status:10} | "
                    f"R{o.total_amount:>7.2f} | {o.created_at.strftime('%d %b %Y')} | "
                    f"{items_str} | {'can cancel' if can_cancel else 'locked'}"
                )
            history_block = (
                f"=== ORDER HISTORY ({len(recent)} orders, R{total_spent:.2f} total) ===\n"
                + "\n".join(lines)
            )
        else:
            history_block = "=== ORDER HISTORY ===\nNo previous orders yet."
    except Exception as e:
        logger.warning(f"Order history fetch failed: {e}")

    driver_block = await _build_driver_block(str(user.id))
    phone = getattr(user, "phone", None) or "Not on file"

    return f"""You are KotaBot, the friendly AI assistant for KotaBites — Johannesburg south's favourite kota delivery service.

Your goals:
1. Help customers track orders and explain statuses
2. Recommend menu items based on history and preferences
3. Accept suggestions, compliments and complaints
4. Answer general questions about KotaBites
5. Help customers cancel orders that are still cancellable
6. Do Not take orders. Guide them through the app https://foodsorder.vercel.app/menu
7. You are built in this website app https://foodsorder.vercel.app
8. Add basic words from SiSwati in the conversations
9. Change language choose only one if user request it: 100% SiSwati ( Not Zulu) , English
10. If the user is a driver (see driver block below), also help them with their earnings, stats and delivery questions

Calculation for Kota points (kp):
1. Add all delivered orders amount spent, status( Delivered) only 
2. Multiple the total by 0.1
3. Present the results in a formal way only points( Eg 34Kp) 
Finally you have Kota points by only calculating it. NOTE: don't show calculations

======
Show or display Order IDs in code form or syntax js, py
=======

=== {hours_block} ===

DELIVERY SCHEDULE:
- Monday to Friday: 09:00 – 17:00 (SAST)
- Saturday: 09:00 – 14:00 (SAST)
- Sunday: CLOSED

If delivery is currently CLOSED, politely tell the user we are closed and when we next open.
If a user tries to order while closed, explain we cannot take orders right now and give the next opening time.

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending   - Waiting for payment confirmation   [CAN cancel]
paid      - Payment done, kitchen starting     [CAN cancel, cancellation fee R9 must be paid on the next Order]
preparing - Being cooked right now             [CANNOT cancel]
ready     - Ready for delivery                 [CANNOT cancel]
delivered - Successfully delivered             [CANNOT cancel]
cancelled - Order was cancelled

{order_block}
{history_block}
{driver_block}
=== CUSTOMER ===
Name : {user.full_name}
Email: {user.email}
Phone: {phone}

=== CANCELLATION RULES ===
- ONLY cancel when status is "pending" or "paid" — fees apply
- Once "preparing", "ready", or "delivered" — no cancellation possible
- ALWAYS ask for confirmation first: "Are you sure you want to cancel order #XXXXXXXX?"
- Only after the customer confirms YES, embed this exact tag in your reply:
  [CANCEL_ORDER:{{full_24_char_order_id}}]
- ALWAYS use the full 24-character ID, and not the 8-char short code
- Example: [CANCEL_ORDER:507f1f77bcf86cd799439011]

=== BEHAVIOUR ===
- Be warm, helpful and concise (max 3 short paragraphs)
- Use proper kasi slang naturally: sho, lekker, eish, ayt, Ola, ohk, yoh, hayibo(🤯) , shame, no stress, straight talk, quick-quick, tight, my bad, vibes
- NEVER invent prices or menu items not listed above
- When customer mentions an order ID, look in history and explain the status
- Thank people warmly for feedback and confirm "I've noted it, sho"
- If order not in history, ask nicely for the full 24-char order ID
- Keep it real — talk like you're from the hood, but still professional
- End with something friendly like "Lekker day ahead" or "Hit me anytime, ayt?"
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "love", "great service", "bad", "slow",
]


def _to_openrouter_messages(messages: List[ChatMessage]) -> List[dict]:
    """Trim, normalise roles, drop leading assistant turns, collapse duplicates."""
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
                temperature=0.1,
                max_tokens=100,
                extra_body={"thinking": {"type": "disabled"}},
            )
            raw = (resp.choices[0].message.content or "").strip("```json").strip("```").strip()
            if raw:
                parsed = json.loads(raw)
                category = parsed.get("category", "general")
                sentiment = parsed.get("sentiment", "neutral")
        except Exception as e:
            logger.warning(f"Suggestion classification failed: {e}")

    try:
        await Suggestion(
            user_id=str(user.id),
            user_email=user.email,
            message=last.strip(),
            category=category,
            sentiment=sentiment,
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
                temperature=0.7,
                max_tokens=4096,
                extra_body={"thinking": {"type": "disabled"}},
                stream=True,
            )
            async for chunk in stream:
                token = chunk.choices[0].delta.content
                if token:
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
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
        "success": True,
        "message": "Your order has been cancelled. Sorry to see it go! 🙏",
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
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=body.message.strip(),
            category=body.category or "general",
            sentiment="neutral",
            created_at=datetime.utcnow(),
        ).insert()
        return {"msg": "Thank you! Your feedback has been received."}
    except Exception as e:
        logger.error(f"Suggestion save failed: {e}")
        raise HTTPException(500, "Failed to save feedback")


@router.get("/suggestions")
async def get_suggestions(
    # FIX Bug 8 (SECURITY): was get_current_user — any authenticated customer could
    # read every suggestion ever submitted by any user. Changed to get_current_admin_user.
    admin_user: User = Depends(get_current_admin_user),
):
    try:
        suggestions = await Suggestion.find_all().to_list(length=500)
        summary = {"positive": 0, "neutral": 0, "negative": 0}
        for s in suggestions:
            key = s.sentiment if s.sentiment in summary else "neutral"
            summary[key] += 1

        return {
            "total": len(suggestions),
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


@router.get("/test-ai")
async def test_ai():
    if not client:
        return {"error": "No client — missing KIMI_API_KEY"}
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "Say yebo"}],
            max_tokens=200,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return {"reply": resp.choices[0].message.content}
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
            max_tokens=200,
            temperature=0.0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return {
            "status": "ok",
            "reply": (resp.choices[0].message.content or "").strip(),
            "usage": resp.usage.model_dump() if resp.usage else None
        }
    except Exception as e:
        return {"status": "error", "detail": str(e), "type": type(e).__name__}
