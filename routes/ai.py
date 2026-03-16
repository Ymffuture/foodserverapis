# routes/ai.py
import os
import json
import logging
import asyncio
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import google.generativeai as genai

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion
from utils.enums import OrderStatus

router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Gemini Setup ───────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL = "gemini-2.5-flash"

# Max conversation turns sent to Gemini — prevents context-window overflow
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

    # ── 1. Menu ──────────────────────────────────────────────────────────────
    try:
        items = await MenuItem.find_all().to_list(length=60)
        if items:
            menu_text = "\n".join(
                f"• {i.name} — R{i.price:.2f} [{i.category}]"
                + (f": {i.description[:100]}" if i.description else "")
                for i in items
            )
        else:
            menu_text = "(Menu currently empty)"
    except Exception as e:
        logger.warning(f"Menu fetch failed: {e}")
        menu_text = "(Menu unavailable)"

    # ── 2. Active order context ───────────────────────────────────────────────
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(
                    f"{it.name} ×{it.quantity}" for it in (order.items or [])
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

    # ── 3. Recent order history ───────────────────────────────────────────────
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
                    f"{it.name} ×{it.quantity}" for it in (o.items or [])
                )
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                can_cancel = o.status in CANCELLABLE_STATUSES
                history_lines.append(
                    f"  • #{str(o.id)[-8:].upper()} (ID:{str(o.id)}) | {status:10} | "
                    f"R{o.total_amount:>7.2f} | {o.created_at.strftime('%d %b %Y')} | "
                    f"{items_str} | {'❌ cancellable' if can_cancel else '🔒 locked'}"
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

    # ── 4. Assemble prompt ────────────────────────────────────────────────────
    return f"""You are KotaBot 🍔🤖 — the friendly AI helper for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
1. Help customers track their orders and understand what each status means
2. Recommend menu items based on their order history and preferences
3. Accept suggestions, compliments or complaints and save them
4. Answer general questions about KotaBites
5. Help customers cancel orders that are still cancellable

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES EXPLAINED ===
pending   → Order placed, waiting for payment confirmation  [CAN cancel]
paid      → Payment confirmed, kitchen will start soon      [CAN cancel]
preparing → Being cooked right now 🍳                       [CANNOT cancel]
ready     → Ready and on its way to you!                    [CANNOT cancel]
delivered → Successfully delivered 🎉                       [CANNOT cancel]
cancelled → Order was cancelled

{order_block}
{history_block}

=== CUSTOMER ===
Name : {user.full_name}
Email: {user.email}
Phone: {getattr(user, 'phone', 'Not on file')}

=== CANCELLATION RULES ===
- Orders can ONLY be cancelled when status is "pending" or "paid"
- Once "preparing", "ready", or "delivered" — cancellation is NOT possible
- To cancel, tell the user: "I'll cancel that for you — just confirm below" and include this EXACT tag in your reply:
  [CANCEL_ORDER:{{full_24_char_order_id}}]
- If the user confirms intent to cancel, include the tag. If they are just asking about cancellation, explain the rules first.
- Always use the full 24-character order ID inside the tag, never the short code.
- Example: [CANCEL_ORDER:507f1f77bcf86cd799439011]

=== YOUR RULES ===
- Be warm, helpful and concise (max 3 short paragraphs per reply)
- Use light SA slang: sharp, lekker, eish, ayt, jozi — but don't overdo it
- NEVER invent prices or menu items not listed above
- When a customer pastes an order ID, look it up from their history above
- If they ask what they usually order, reference their history above
- If user gives feedback/suggestion → thank them warmly and say you've noted it
- If asked about an order not in their history, ask them to paste the full Order ID
- Always confirm before cancelling — ask "Are you sure you want to cancel order #XXXXXXXX?"
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "love", "great service", "bad", "slow",
]


def _safe_response_text(response) -> str:
    """
    response.text raises ValueError when Gemini blocks or returns empty candidates.
    Always extract text safely so the endpoint never crashes mid-conversation.
    """
    try:
        text = response.text
        if text:
            return text.strip()
    except (ValueError, AttributeError):
        pass

    # Fallback: walk candidates manually
    try:
        for candidate in (response.candidates or []):
            parts = getattr(candidate.content, "parts", [])
            combined = "".join(getattr(p, "text", "") for p in parts).strip()
            if combined:
                return combined
    except Exception:
        pass

    return ""


def _to_gemini_messages(messages: List[ChatMessage]) -> List[dict]:
    """
    Gemini requires:
      • First message must be role "user" (not "model")
      • No two consecutive messages with the same role
    Also trims to MAX_HISTORY_TURNS to avoid context-window overflow.
    """
    trimmed = messages[-MAX_HISTORY_TURNS:] if len(messages) > MAX_HISTORY_TURNS else messages

    result = []
    for m in trimmed:
        result.append({
            "role": "user" if m.role == "user" else "model",
            "parts": [{"text": m.content}],
        })

    # Drop any leading "model" turns
    while result and result[0]["role"] == "model":
        result.pop(0)

    # Collapse consecutive same-role messages
    deduped: List[dict] = []
    for turn in result:
        if deduped and deduped[-1]["role"] == turn["role"]:
            deduped[-1]["parts"][0]["text"] += "\n" + turn["parts"][0]["text"]
        else:
            deduped.append(turn)

    return deduped


def _extract_cancel_order_id(reply: str) -> Optional[str]:
    """
    Parse [CANCEL_ORDER:<24-char-id>] tags that KotaBot embeds in its reply
    when it decides to trigger a cancellation.
    """
    import re
    match = re.search(r"\[CANCEL_ORDER:([0-9a-fA-F]{24})\]", reply)
    return match.group(1) if match else None


async def _maybe_save_suggestion(messages: List[ChatMessage], user: User):
    """Auto-detect and save feedback with AI sentiment/category classification."""
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if not any(kw in last.lower() for kw in SUGGESTION_KEYWORDS):
        return

    category, sentiment = "general", "neutral"
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        meta = await asyncio.to_thread(
            model.generate_content,
            f'Classify this customer feedback. Reply ONLY with valid JSON, no markdown, no explanation: '
            f'{{"category": "<food|service|app|general>", "sentiment": "<positive|neutral|negative>"}}\n'
            f'Feedback: "{last[:300]}"'
        )
        raw = _safe_response_text(meta).strip("```json").strip("```").strip()
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
    """Non-streaming KotaBot chat — fetches real menu + order data."""
    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI service not configured — GEMINI_API_KEY missing")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    gemini_messages = _to_gemini_messages(req.messages)

    if not gemini_messages:
        return {"reply": "Yebo! How can I help you today?"}

    try:
        model = genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=system_prompt,
        )

        response = await asyncio.to_thread(
            model.generate_content,
            contents=gemini_messages,
            generation_config=genai.GenerationConfig(
                temperature=0.7,
                max_output_tokens=600,
            ),
        )

        reply = _safe_response_text(response)
        if not reply:
            reply = "Eish, I couldn't generate a reply right now. Please try again!"

        # ── Auto-cancel if KotaBot embedded a [CANCEL_ORDER:...] tag ──────────
        cancel_id = _extract_cancel_order_id(reply)
        cancel_result: Optional[dict] = None

        if cancel_id:
            # Strip the tag from the visible reply
            import re
            reply = re.sub(r"\[CANCEL_ORDER:[0-9a-fA-F]{24}\]", "", reply).strip()

            try:
                order = await Order.get(cancel_id)
                if not order:
                    cancel_result = {"success": False, "reason": "Order not found"}
                elif order.user_id != str(current_user.id):
                    cancel_result = {"success": False, "reason": "Not your order"}
                elif order.status not in CANCELLABLE_STATUSES:
                    status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
                    cancel_result = {
                        "success": False,
                        "reason": f"Cannot cancel — order is already '{status_val}'",
                    }
                else:
                    order.status = OrderStatus.CANCELLED
                    await order.save()
                    cancel_result = {
                        "success": True,
                        "order_id": cancel_id,
                        "short_id": cancel_id[-8:].upper(),
                    }
                    logger.info(f"Order {cancel_id} cancelled via KotaBot by {current_user.email}")
            except Exception as e:
                logger.error(f"Auto-cancel failed for {cancel_id}: {e}")
                cancel_result = {"success": False, "reason": "Cancellation failed — please try again"}

        await _maybe_save_suggestion(req.messages, current_user)

        response_payload: dict = {"reply": reply}
        if cancel_result is not None:
            response_payload["cancel_result"] = cancel_result

        return response_payload

    except Exception as e:
        logger.exception("Gemini /chat error")
        raise HTTPException(500, f"AI service error: {str(e)}")


@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """SSE streaming KotaBot."""
    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI service not configured — GEMINI_API_KEY missing")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    gemini_messages = _to_gemini_messages(req.messages)

    if not gemini_messages:
        async def _empty():
            yield f"data: {json.dumps({'token': 'Yebo! How can I help you today?'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_empty(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _run_stream():
        try:
            model = genai.GenerativeModel(
                model_name=MODEL,
                system_instruction=system_prompt,
            )
            stream = model.generate_content(
                contents=gemini_messages,
                generation_config=genai.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=600,
                ),
                stream=True,
            )
            for chunk in stream:
                text = ""
                try:
                    text = chunk.text or ""
                except (ValueError, AttributeError):
                    pass
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, text)
        except Exception as e:
            logger.exception("Gemini stream thread error")
            loop.call_soon_threadsafe(queue.put_nowait, None)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, "<<DONE>>")

    async def event_generator() -> AsyncGenerator[str, None]:
        _future = loop.run_in_executor(None, _run_stream)  # noqa: F841
        while True:
            token = await queue.get()
            if token == "<<DONE>>":
                yield "data: [DONE]\n\n"
                break
            if token is None:
                yield f"data: {json.dumps({'error': 'AI stream error'})}\n\n"
                break
            yield f"data: {json.dumps({'token': token})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/cancel-order")
async def cancel_order_via_chat(
    body: CancelOrderRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Direct cancel-order endpoint called by the frontend when the user
    explicitly confirms cancellation (e.g. clicks a Cancel button in chat).

    Business rules:
      • Only the order owner can cancel
      • Only 'pending' or 'paid' orders can be cancelled
      • 'preparing', 'ready', 'delivered' are locked
    """
    try:
        order = await Order.get(body.order_id)
    except Exception:
        raise HTTPException(404, "Order not found")

    if not order:
        raise HTTPException(404, "Order not found")

    if order.user_id != str(current_user.id):
        raise HTTPException(403, "You can only cancel your own orders")

    if order.status not in CANCELLABLE_STATUSES:
        status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
        raise HTTPException(
            409,
            f"Cannot cancel — order is already '{status_val}'. "
            "Only pending or paid orders can be cancelled."
        )

    order.status = OrderStatus.CANCELLED
    await order.save()

    logger.info(
        f"Order {body.order_id} cancelled by {current_user.email}"
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
