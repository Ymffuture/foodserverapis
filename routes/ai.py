# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion
from utils.enums import OrderStatus

router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── OpenRouter + Kimi K2.5 Setup ───────────────────────────────────────────
# Add KIMI_API_KEY=sk-or-... to your .env (OpenRouter key)
KIMI_API_KEY = os.getenv("KIMI_API_KEY")

MODEL = "moonshotai/kimi-k2:free"  # OpenRouter model ID for Kimi K2.5

openrouter_client: Optional[AsyncOpenAI] = None
if KIMI_API_KEY:
    openrouter_client = AsyncOpenAI(
        api_key=KIMI_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://foodsorder.vercel.app",
            "X-Title": "kotabots",
        },
    )

# Max conversation turns — prevents context-window overflow
MAX_HISTORY_TURNS = 20

# Statuses that are still cancellable
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
    order_id: str = Field(..., min_length=1)
    reason: Optional[str] = Field(default=None, max_length=500)


# ── System Prompt ──────────────────────────────────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:

    # ── 1. Menu ───────────────────────────────────────────────────────────────
    try:
        items = await MenuItem.find_all().to_list(length=60)
        if items:
            menu_text = "\n".join(
                f"- {i.name} — R{i.price:.2f} [{i.category}]"
                + (f": {i.description[:100]}" if i.description else "")
                for i in items
            )
        else:
            menu_text = "(Menu currently empty)"
    except Exception as e:
        logger.warning(f"Menu fetch failed: {e}")
        menu_text = "(Menu unavailable)"

    # ── 2. Active order context ────────────────────────────────────────────────
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
Can cancel: {"YES — status is still " + status_val if can_cancel else "NO — already " + status_val}
"""
        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    # ── 3. Recent order history ────────────────────────────────────────────────
    history_block = ""
    try:
        recent_orders = await Order.find(
            Order.user_id == str(user.id)
        ).to_list(length=10)

        if recent_orders:
            recent_orders.sort(key=lambda o: o.created_at, reverse=True)
            history_lines = []
            total_spent = 0.0
            for o in recent_orders:
                total_spent += o.total_amount
                items_str = ", ".join(
                    f"{it.name} x{it.quantity}" for it in (o.items or [])
                )
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                can_cancel = o.status in CANCELLABLE_STATUSES
                history_lines.append(
                    f"  - #{str(o.id)[-8:].upper()} (ID:{str(o.id)}) | {status:10} | "
                    f"R{o.total_amount:>7.2f} | {o.created_at.strftime('%d %b %Y')} | "
                    f"{items_str} | {'cancellable' if can_cancel else 'locked'}"
                )
            history_block = f"""
=== ORDER HISTORY ({len(recent_orders)} orders, R{total_spent:.2f} total spent) ===
{chr(10).join(history_lines)}
"""
        else:
            history_block = "\n=== ORDER HISTORY ===\nNo previous orders yet.\n"
    except Exception as e:
        logger.warning(f"Order history fetch failed: {e}")
        history_block = ""

    return f"""You are KotaBot, the friendly AI assistant for KotaBites — Johannesburg's favourite kota delivery service.

Your goals:
1. Help customers track their orders and understand status updates
2. Recommend menu items based on order history and preferences
3. Save suggestions, compliments and complaints
4. Answer general questions about KotaBites
5. Help customers cancel orders that are still cancellable

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending   - Order placed, awaiting payment confirmation  [CAN cancel]
paid      - Payment confirmed, kitchen starting soon     [CAN cancel]
preparing - Being cooked right now                       [CANNOT cancel]
ready     - Ready for delivery                           [CANNOT cancel]
delivered - Successfully delivered                       [CANNOT cancel]
cancelled - Order was cancelled

{order_block}
{history_block}

=== CUSTOMER ===
Name : {user.full_name}
Email: {user.email}
Phone: {getattr(user, 'phone', 'Not on file')}

=== CANCELLATION RULES ===
- ONLY cancel when status is "pending" or "paid"
- Once "preparing", "ready", or "delivered" — no cancellation possible
- After the user CONFIRMS they want to cancel, embed this tag in your reply:
  [CANCEL_ORDER:{{full_24_char_order_id}}]
- ALWAYS use the full 24-character MongoDB ObjectId, never the 8-char short code
- Example: [CANCEL_ORDER:507f1f77bcf86cd799439011]
- ALWAYS ask for confirmation first before embedding the tag

=== BEHAVIOUR RULES ===
- Be warm, helpful and concise (max 3 short paragraphs)
- Use light SA slang: sharp, lekker, eish, ayt — but don't overdo it
- NEVER invent prices or menu items not listed above
- Look up order IDs from the history above when customers mention them
- Reference order history when asked what they usually order
- Thank users warmly for feedback and confirm you have noted it
- If an order ID is not in history, ask them to paste the full 24-char ID
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "love", "great service", "bad", "slow",
]


def _to_openrouter_messages(messages: List[ChatMessage]) -> List[dict]:
    """
    Convert frontend ChatMessage list to OpenAI-format dicts.
    - Trims to MAX_HISTORY_TURNS
    - Drops leading assistant turns (API requires user turn first)
    - Collapses consecutive same-role messages
    """
    trimmed = (
        messages[-MAX_HISTORY_TURNS:]
        if len(messages) > MAX_HISTORY_TURNS
        else messages
    )

    result = [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in trimmed
    ]

    # Drop leading assistant turns
    while result and result[0]["role"] == "assistant":
        result.pop(0)

    # Collapse consecutive same-role messages
    deduped: List[dict] = []
    for turn in result:
        if deduped and deduped[-1]["role"] == turn["role"]:
            deduped[-1]["content"] += "\n" + turn["content"]
        else:
            deduped.append(turn)

    return deduped


def _extract_cancel_order_id(reply: str) -> Optional[str]:
    """Parse [CANCEL_ORDER:<24-char-id>] tags embedded by KotaBot."""
    import re
    match = re.search(r"\[CANCEL_ORDER:([0-9a-fA-F]{24})\]", reply)
    return match.group(1) if match else None


async def _maybe_save_suggestion(messages: List[ChatMessage], user: User) -> None:
    """Auto-detect feedback and save with AI sentiment/category classification."""
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if not any(kw in last.lower() for kw in SUGGESTION_KEYWORDS):
        return

    category, sentiment = "general", "neutral"
    if openrouter_client:
        try:
            resp = await openrouter_client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Classify this customer feedback. "
                            "Reply ONLY with valid JSON, no markdown, no explanation:\n"
                            '{"category": "<food|service|app|general>", "sentiment": "<positive|neutral|negative>"}\n'
                            f'Feedback: "{last[:300]}"'
                        ),
                    }
                ],
                temperature=0.1,
                max_tokens=60,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = raw.strip("```json").strip("```").strip()
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


async def _execute_cancel(order_id: str, user: User) -> dict:
    """Shared cancel logic used by both /chat and /cancel-order endpoints."""
    try:
        order = await Order.get(order_id)
    except Exception:
        return {"success": False, "reason": "Order not found"}

    if not order:
        return {"success": False, "reason": "Order not found"}
    if order.user_id != str(user.id):
        return {"success": False, "reason": "Not your order"}
    if order.status not in CANCELLABLE_STATUSES:
        status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
        return {"success": False, "reason": f"Cannot cancel — order is already '{status_val}'"}

    order.status = OrderStatus.CANCELLED
    await order.save()
    logger.info(f"Order {order_id} cancelled by {user.email}")
    return {
        "success": True,
        "order_id": order_id,
        "short_id": order_id[-8:].upper(),
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Non-streaming KotaBot chat — powered by Kimi K2.5 via OpenRouter."""
    if not openrouter_client:
        raise HTTPException(503, "AI service not configured — KIMI_API_KEY missing")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    chat_messages = _to_openrouter_messages(req.messages)

    if not chat_messages:
        return {"reply": "Yebo! How can I help you today?"}

    full_messages = [{"role": "system", "content": system_prompt}] + chat_messages

    try:
        response = await openrouter_client.chat.completions.create(
            model=MODEL,
            messages=full_messages,
            temperature=0.7,
            max_tokens=600,
        )

        reply = (response.choices[0].message.content or "").strip()
        if not reply:
            reply = "Eish, I couldn't generate a reply right now. Please try again!"

        # Auto-execute cancel if KotaBot embedded a [CANCEL_ORDER:...] tag
        cancel_id = _extract_cancel_order_id(reply)
        cancel_result: Optional[dict] = None

        if cancel_id:
            import re
            reply = re.sub(r"\[CANCEL_ORDER:[0-9a-fA-F]{24}\]", "", reply).strip()
            cancel_result = await _execute_cancel(cancel_id, current_user)

        await _maybe_save_suggestion(req.messages, current_user)

        payload: dict = {"reply": reply}
        if cancel_result is not None:
            payload["cancel_result"] = cancel_result

        return payload

    except Exception as e:
        logger.exception("OpenRouter /chat error")
        raise HTTPException(500, f"AI service error: {str(e)}")


@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """SSE streaming KotaBot — Kimi K2.5 via OpenRouter (natively async)."""
    if not openrouter_client:
        raise HTTPException(503, "AI service not configured — KIMI_API_KEY missing")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    chat_messages = _to_openrouter_messages(req.messages)

    if not chat_messages:
        async def _empty() -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'token': 'Yebo! How can I help you today?'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")

    full_messages = [{"role": "system", "content": system_prompt}] + chat_messages

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            stream = await openrouter_client.chat.completions.create(
                model=MODEL,
                messages=full_messages,
                temperature=0.7,
                max_tokens=600,
                stream=True,
            )
            async for chunk in stream:
                token = chunk.choices[0].delta.content
                if token:
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
            logger.exception("OpenRouter stream error")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/cancel-order")
async def cancel_order_via_chat(
    body: CancelOrderRequest,
    current_user: User = Depends(get_current_user),
):
    """Direct cancel endpoint — called when user clicks confirm in the chat UI."""
    result = await _execute_cancel(body.order_id, current_user)

    if not result["success"]:
        reason = result.get("reason", "")
        if "not found" in reason.lower():
            raise HTTPException(404, reason)
        if "not your order" in reason.lower():
            raise HTTPException(403, reason)
        raise HTTPException(409, reason)

    logger.info(
        f"Order {body.order_id} cancelled via direct endpoint by {current_user.email}"
        + (f" — reason: {body.reason}" if body.reason else "")
    )

    return {
        "success": True,
        "message": "Your order has been cancelled. Sorry to see it go!",
        "order_id": body.order_id,
        "short_id": body.order_id[-8:].upper(),
    }


@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user),
):
    """Directly submit a suggestion or piece of feedback."""
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
async def get_suggestions(current_user: User = Depends(get_current_user)):
    """Admin: all suggestions with sentiment breakdown."""
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
        raise HTTPException(500, "Failed to retrieve suggestions")
