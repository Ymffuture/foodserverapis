# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
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

# ── OpenRouter + Kimi Setup ───────────────────────────────────────────────
KIMI_API_KEY = os.getenv("KIMI_API_KEY")

# Correct working model (March 2026)
MODEL = "moonshotai/kimi-k2.5"

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

MAX_HISTORY_TURNS = 20


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


# ── System Prompt (your excellent version kept) ────────────────────────────
async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:
    # Menu
    try:
        items = await MenuItem.find_all().limit(60).to_list()
        menu_text = "\n".join(
            f"- {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description[:100]}" if i.description else "")
            for i in items
        ) or "(Menu currently empty)"
    except Exception:
        menu_text = "(Menu unavailable)"

    # Active order
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(f"{it.name} x{it.quantity}" for it in (order.items or []))
                status_val = order.status.value if hasattr(order.status, "value") else str(order.status)
                order_block = f"""
=== ACTIVE ORDER ===
Order #{str(order.id)[-8:].upper()} — {status_val.upper()}
Total   : R{order.total_amount:.2f}
Items   : {items_str or "none"}
Address : {order.delivery_address or "Not specified"}
"""
        except Exception:
            pass

    # Recent history
    history_block = ""
    try:
        recent = await Order.find(Order.user_id == str(user.id)).limit(10).to_list()
        if recent:
            history_block = "Recent orders:\n" + "\n".join(
                f"  • #{str(o.id)[-8:].upper()} — {o.status} — R{o.total_amount:.2f}"
                for o in recent
            )
        else:
            history_block = "No previous orders yet."
    except Exception:
        history_block = ""

    return f"""You are KotaBot 🍔🤖 — the friendly AI assistant for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
- Help customers track orders and explain statuses
- Recommend menu items
- Accept suggestions and complaints
- Answer general questions

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending   - Waiting for payment
paid      - Kitchen starting soon
preparing - Being cooked
ready     - Ready for delivery
delivered - Delivered
cancelled - Cancelled

{order_block}
{history_block}

Customer: {user.full_name} ({user.email})

Rules:
- Be warm, helpful and concise (max 3 short paragraphs)
- Use light SA slang: sharp, lekker, eish, ayt, jozi
- Never invent prices or items
- For feedback: thank them and say it's noted
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = ["suggest", "feedback", "complaint", "improve", "issue", "problem", "eish", "not happy"]


def _to_openrouter_messages(messages: List[ChatMessage]) -> List[dict]:
    trimmed = messages[-MAX_HISTORY_TURNS:] if len(messages) > MAX_HISTORY_TURNS else messages
    return [
        {"role": "user" if m.role == "user" else "assistant", "content": m.content}
        for m in trimmed
    ]


async def _maybe_save_suggestion(messages: List[ChatMessage], user: User):
    last = next((m.content for m in reversed(messages) if m.role == "user"), "")
    if any(kw in last.lower() for kw in SUGGESTION_KEYWORDS):
        try:
            await Suggestion(
                user_id=str(user.id),
                user_email=user.email,
                message=last.strip(),
                category="general",
                sentiment="neutral",
                created_at=datetime.utcnow(),
            ).insert()
        except Exception as e:
            logger.warning(f"Failed to save suggestion: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not client:
        raise HTTPException(503, "AI service not configured — add KIMI_API_KEY")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    messages = _to_openrouter_messages(req.messages)

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            temperature=0.7,
            max_tokens=600,
        )

        reply = (response.choices[0].message.content or "").strip()

        await _maybe_save_suggestion(req.messages, current_user)

        return {"reply": reply or "Eish, I couldn't generate a reply. Try again!"}

    except Exception as e:
        logger.exception("AI chat error")
        raise HTTPException(500, "AI service error. Please try again.")


@router.post("/chat/stream")
async def ai_chat_stream(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not client:
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)
    messages = _to_openrouter_messages(req.messages)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            stream = await client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                temperature=0.7,
                max_tokens=600,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield f"data: {json.dumps({'token': chunk.choices[0].delta.content})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception("Streaming error")
            yield f"data: {json.dumps({'error': 'AI service error'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/suggestion", status_code=201)
async def save_suggestion(body: SuggestionRequest, current_user: User = Depends(get_current_user)):
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
    try:
        suggestions = await Suggestion.find_all().to_list()
        summary = {"positive": 0, "neutral": 0, "negative": 0}
        for s in suggestions:
            summary[s.sentiment or "neutral"] += 1

        return {
            "total": len(suggestions),
            "sentiment_summary": summary,
            "items": [
                {
                    "id": str(s.id),
                    "email": s.user_email,
                    "message": s.message,
                    "category": s.category,
                    "sentiment": s.sentiment,
                    "created_at": s.created_at,
                }
                for s in suggestions
            ],
        }
    except Exception as e:
        logger.error(f"Get suggestions failed: {e}")
        raise HTTPException(500, "Could not load suggestions")
