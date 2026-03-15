# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import google.generativeai as genai
from fastapi.concurrency import run_in_threadpool

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion

router = APIRouter(tags=["AI Assistant"])   # ← NO prefix here

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Gemini Setup ───────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL = "gemini-2.5-flash"   # Change to "gemini-1.5-pro" if you want better quality


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
    # Menu
    try:
        items = await MenuItem.find_all().limit(60).to_list()
        menu_text = "\n".join(
            f"• {i.name} — R{i.price:.2f} [{i.category}]"
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
                order_block = f"""
Active order:
Order #{str(order.id)[-8:].upper()} — {order.status.upper()}
Total: R{order.total_amount:.2f}
Items: {items_str}
Address: {order.delivery_address or "Not specified"}
"""
        except Exception:
            pass

    return f"""You are KotaBot 🍔🤖 — the friendly AI helper for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
- Help customers track orders and understand statuses
- Recommend menu items
- Accept suggestions, compliments or complaints
- Answer general questions about KotaBites

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending → Waiting for payment | paid → Kitchen starting soon
preparing → Being cooked 🍳 | ready → Ready for delivery
delivered → Delivered 🎉 | cancelled → Order cancelled

{order_block}

Customer: {user.full_name} ({user.email})

Rules:
- Be warm, helpful, concise (max 3 short paragraphs)
- Use light SA slang sometimes: sharp, lekker, eish, ayt, jozi
- Never invent prices or items
- If user gives feedback/suggestion → thank them warmly and say it's noted
"""


# ── Helpers ────────────────────────────────────────────────────────────────
SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy"
]


def _to_gemini_messages(messages: List[ChatMessage]) -> List[dict]:
    """Convert to Gemini format"""
    return [
        {"role": "user" if m.role == "user" else "model", "parts": [m.content]}
        for m in messages
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
            logger.warning(f"Failed to auto-save suggestion: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not GEMINI_API_KEY:
        raise HTTPException(503, "Gemini API key not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    try:
        model = genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=system_prompt,
        )

        response = model.generate_content(
            contents=_to_gemini_messages(req.messages),
            generation_config=genai.GenerationConfig(
                temperature=0.7,
                max_output_tokens=600,
            ),
        )

        reply = (response.text or "").strip() or "Sorry, I couldn't generate a reply right now."

        await _maybe_save_suggestion(req.messages, current_user)
        return {"reply": reply}

    except Exception as e:
        logger.exception("Gemini /chat error")
        raise HTTPException(500, "AI service error. Please try again.")


@router.post("/chat/stream")
async def ai_chat_stream(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not GEMINI_API_KEY:
        raise HTTPException(503, "Gemini API key not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            model = genai.GenerativeModel(
                model_name=MODEL,
                system_instruction=system_prompt,
            )

            # Gemini streaming is synchronous → run in threadpool
            stream = await run_in_threadpool(
                model.generate_content,
                contents=_to_gemini_messages(req.messages),
                generation_config=genai.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=600,
                ),
                stream=True,
            )

            for chunk in stream:
                if chunk.text:
                    yield f"data: {json.dumps({'token': chunk.text})}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Gemini stream error")
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
