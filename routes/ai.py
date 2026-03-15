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

router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Gemini Setup ───────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL = "gemini-1.5-flash"


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


# ── System Prompt ──────────────────────────────────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:

    # ── 1. Menu ─────────────────────────────────────────────────────────────
    # ✅ FIX: correct Beanie API — to_list(length=N), NOT .limit().to_list()
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

    # ── 2. Active order context (if user is on the order page) ──────────────
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(
                    f"{it.name} ×{it.quantity}" for it in (order.items or [])
                )
                order_block = f"""
=== ACTIVE ORDER ===
Order #{str(order.id)[-8:].upper()} — Status: {order.status.upper() if hasattr(order.status, 'upper') else str(order.status).upper()}
Total : R{order.total_amount:.2f}
Items : {items_str or "none"}
Payment: {order.payment_method or "paystack"}
Address: {order.delivery_address or "Not specified"}
"""
        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    # ── 3. Recent order history ──────────────────────────────────────────────
    # ✅ FIX: always fetch the user's recent orders so KotaBot can reference
    # their history, total spend, favourite items, etc.
    history_block = ""
    try:
        recent_orders = await Order.find(
            Order.user_id == str(user.id)
        ).to_list(length=10)

        if recent_orders:
            # Sort by created_at descending (most recent first)
            recent_orders.sort(key=lambda o: o.created_at, reverse=True)

            history_lines = []
            total_spent = 0.0
            for o in recent_orders:
                total_spent += o.total_amount
                items_str = ", ".join(
                    f"{it.name} ×{it.quantity}" for it in (o.items or [])
                )
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                history_lines.append(
                    f"  • #{str(o.id)[-8:].upper()} | {status:10} | R{o.total_amount:>7.2f} | {o.created_at.strftime('%d %b %Y')} | {items_str}"
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

    # ── 4. Assemble prompt ───────────────────────────────────────────────────
    return f"""You are KotaBot 🍔🤖 — the friendly AI helper for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
1. Help customers track their orders and understand what each status means
2. Recommend menu items based on their order history and preferences
3. Accept suggestions, compliments or complaints and save them
4. Answer general questions about KotaBites

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES EXPLAINED ===
pending   → Order placed, waiting for payment confirmation
paid      → Payment confirmed, kitchen will start soon
preparing → Being cooked right now 🍳
ready     → Ready and on its way to you!
delivered → Successfully delivered 🎉
cancelled → Order was cancelled

{order_block}
{history_block}

=== CUSTOMER ===
Name : {user.full_name}
Email: {user.email}

=== YOUR RULES ===
- Be warm, helpful and concise (max 3 short paragraphs per reply)
- Use light SA slang: sharp, lekker, eish, ayt, jozi — but don't overdo it
- NEVER invent prices or menu items not listed above
- When a customer pastes an order ID (full 24-char or 8-char short code), look it up from their history above
- If a customer mentions a specific order, use the history above to give them real details
- If they ask what they usually order, reference their history above
- If user gives feedback/suggestion → thank them warmly and say you've noted it
- If asked about an order not in their history, ask them to paste the full Order ID
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "love", "great service", "bad", "slow",
]


def _to_gemini_messages(messages: List[ChatMessage]) -> List[dict]:
    """
    ✅ FIX: correct Gemini SDK format.
    Was: "parts": [m.content]  ← string, not a Part dict → malformed input
    Now: "parts": [{"text": m.content}]  ← correct
    """
    result = []
    for m in messages:
        result.append({
            "role": "user" if m.role == "user" else "model",
            "parts": [{"text": m.content}],
        })
    return result


async def _maybe_save_suggestion(messages: List[ChatMessage], user: User):
    """Auto-detect and save feedback with AI sentiment/category classification."""
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if not any(kw in last.lower() for kw in SUGGESTION_KEYWORDS):
        return

    # Quick AI classification
    category, sentiment = "general", "neutral"
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")
        meta = await asyncio.to_thread(
            model.generate_content,
            f'Classify this customer feedback. Reply ONLY with valid JSON, no markdown, no explanation: '
            f'{{"category": "<food|service|app|general>", "sentiment": "<positive|neutral|negative>"}}\n'
            f'Feedback: "{last[:300]}"'
        )
        raw = meta.text.strip().strip("```json").strip("```").strip()
        parsed = json.loads(raw)
        category  = parsed.get("category", "general")
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

    try:
        model = genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=system_prompt,
        )

        # ✅ Run blocking Gemini call in a thread — never blocks the event loop
        response = await asyncio.to_thread(
            model.generate_content,
            contents=_to_gemini_messages(req.messages),
            generation_config=genai.GenerationConfig(
                temperature=0.7,
                max_output_tokens=600,
            ),
        )

        reply = (response.text or "").strip()
        if not reply:
            reply = "Eish, I couldn't generate a reply right now. Please try again!"

        await _maybe_save_suggestion(req.messages, current_user)
        return {"reply": reply}

    except Exception as e:
        logger.exception("Gemini /chat error")
        raise HTTPException(500, f"AI service error: {str(e)}")


@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """SSE streaming KotaBot — correct queue-based approach."""
    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI service not configured — GEMINI_API_KEY missing")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    # ✅ FIX: use asyncio.Queue to bridge the sync Gemini stream
    # and the async SSE generator — never blocks the event loop
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _run_stream():
        try:
            model = genai.GenerativeModel(
                model_name=MODEL,
                system_instruction=system_prompt,
            )
            stream = model.generate_content(
                contents=_to_gemini_messages(req.messages),
                generation_config=genai.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=600,
                ),
                stream=True,
            )
            for chunk in stream:
                if chunk.text:
                    loop.call_soon_threadsafe(queue.put_nowait, chunk.text)
        except Exception as e:
            logger.exception("Gemini stream thread error")
            loop.call_soon_threadsafe(queue.put_nowait, None)  # signal error
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, "<<DONE>>")

    async def event_generator() -> AsyncGenerator[str, None]:
        loop.run_in_executor(None, _run_stream)
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
        # ✅ FIX: correct Beanie API
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
