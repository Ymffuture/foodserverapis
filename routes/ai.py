# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import google.generativeai as genai

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion

# ── FIX 1: NO prefix here — main.py already registers with prefix="/ai"
# Old code had prefix="/ai" here too → every endpoint became /ai/ai/chat (404)
router = APIRouter(tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── FIX 2: google-generativeai package (NOT google-genai)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL = "gemini-1.5-flash"


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    order_id: Optional[str] = None


class SuggestionRequest(BaseModel):
    message: str = Field(..., min_length=5, max_length=2000)
    category: Optional[str] = Field(default="general", max_length=50)


# ── System Prompt ─────────────────────────────────────────────────────────────

async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:
    try:
        items = await MenuItem.find_all().to_list()
        menu_text = "\n".join(
            f"• {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description[:100]}" if i.description else "")
            for i in items[:60]
        ) or "(Menu currently empty)"
    except Exception:
        menu_text = "(Menu unavailable)"

    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(f"{it.name} x{it.quantity}" for it in (order.items or []))
                order_block = f"""
Active order:
Order #{str(order.id)[-8:].upper()} — {order.status.upper()}
Total: R{order.total_amount:.2f} | Items: {items_str}
Address: {order.delivery_address or "Not specified"}
"""
        except Exception:
            pass

    try:
        recent = await Order.find(Order.user_id == str(user.id)).limit(5).to_list()
        history_block = "Recent orders:\n" + "\n".join(
            f"  • #{str(o.id)[-8:].upper()} — {o.status} — R{o.total_amount:.2f}"
            for o in recent
        ) if recent else "No previous orders."
    except Exception:
        history_block = ""

    return f"""You are KotaBot 🍔🤖 — the friendly AI helper for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
- Help customers track orders and understand statuses
- Recommend menu items based on preferences
- Accept and acknowledge suggestions, compliments or complaints
- Answer general questions about KotaBites

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending → Waiting for payment | paid → Kitchen starting soon
preparing → Being cooked 🍳 | ready → Ready for delivery
delivered → Delivered 🎉 | cancelled → Order cancelled

{order_block}
{history_block}

Customer: {user.full_name} ({user.email})

Rules:
- Warm, helpful, concise (max 3 short paragraphs)
- Light SA slang occasionally: sharp, lekker, eish, ayt, jozi
- Never invent prices or items not listed above
- For feedback/suggestions: thank them warmly and say it is noted
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

SUGGESTION_KEYWORDS = [
    "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
    "add", "missing", "should have", "problem", "issue", "eish", "not happy",
    "disappointed", "slow", "wrong order",
]


def _to_gemini_messages(messages: List[ChatMessage]) -> List[dict]:
    """Convert to Gemini format. Roles must be 'user'/'model', must start with 'user'."""
    result = [
        {"role": "user" if m.role == "user" else "model", "parts": [m.content]}
        for m in messages
    ]
    # Gemini requires conversation to start with user
    while result and result[0]["role"] != "user":
        result.pop(0)
    return result or [{"role": "user", "parts": ["Hello"]}]


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
            logger.warning(f"Could not auto-save suggestion: {e}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI service not configured — set GEMINI_API_KEY on Render")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    try:
        # ── FIX 3: Correct google-generativeai SDK ──
        # system_instruction goes into GenerativeModel constructor
        model = genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=system_prompt,
        )
        response = model.generate_content(
            contents=_to_gemini_messages(req.messages),
            generation_config=genai.GenerationConfig(
                temperature=0.7,
                max_output_tokens=600,
                candidate_count=1,
            ),
        )
        reply = (response.text or "").strip() or "Eish, couldn't generate a response. Try again!"
        await _maybe_save_suggestion(req.messages, current_user)
        return {"reply": reply}

    except Exception as e:
        logger.exception("Gemini chat error")
        s = str(e).lower()
        if "quota" in s or "rate" in s:
            raise HTTPException(429, "Rate limit reached — try again in a moment")
        if "api_key" in s or "api key" in s:
            raise HTTPException(503, "Invalid Gemini API key — check GEMINI_API_KEY env var")
        raise HTTPException(500, f"AI error: {str(e)[:120]}")


@router.post("/chat/stream")
async def ai_chat_stream(req: ChatRequest, current_user: User = Depends(get_current_user)):
    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    def generate():
        try:
            model = genai.GenerativeModel(
                model_name=MODEL,
                system_instruction=system_prompt,
            )
            for chunk in model.generate_content(
                contents=_to_gemini_messages(req.messages),
                generation_config=genai.GenerationConfig(temperature=0.7, max_output_tokens=600),
                stream=True,
            ):
                if chunk.text:
                    yield f"data: {json.dumps({'token': chunk.text})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)[:120]})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/suggestion", status_code=201)
async def save_suggestion(body: SuggestionRequest, current_user: User = Depends(get_current_user)):
    try:
        await Suggestion(
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=body.message.strip(),
            category=(body.category or "general").strip(),
            sentiment="neutral",
            created_at=datetime.utcnow(),
        ).insert()
        return {"msg": "Sharp! Your feedback has been received. 🙏"}
    except Exception as e:
        logger.error(f"Suggestion save failed: {e}")
        raise HTTPException(500, "Failed to save feedback")


@router.get("/suggestions")
async def get_suggestions(current_user: User = Depends(get_current_user)):
    try:
        suggestions = await Suggestion.find_all().to_list()
        suggestions.sort(key=lambda s: s.created_at or datetime.min, reverse=True)
        suggestions = suggestions[:100]
        summary = {"positive": 0, "neutral": 0, "negative": 0}
        for s in suggestions:
            summary[s.sentiment if s.sentiment in summary else "neutral"] += 1
        return {
            "total": len(suggestions),
            "sentiment_summary": summary,
            "items": [
                {
                    "id": str(s.id), "email": s.user_email, "message": s.message,
                    "category": s.category, "sentiment": s.sentiment,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in suggestions
            ],
        }
    except Exception as e:
        raise HTTPException(500, "Could not load suggestions")
