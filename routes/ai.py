# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from google import genai
from google.genai import types

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion

router = APIRouter(prefix="/ai", tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Gemini Client ─────────────────────────────────────────────────────────

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Choose model (gemini-1.5-flash is fast & cheap, gemini-1.5-pro is stronger)
MODEL = "gemini-1.5-flash"          # ← change to "gemini-1.5-pro" if you want better quality


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


# ── System Prompt Builder ─────────────────────────────────────────────────

async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:
    # Menu (limited to avoid huge context)
    try:
        items = await MenuItem.find_all().limit(60).to_list()
        menu_text = "\n".join(
            f"• {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description[:100]}" if i.description else "")
            for i in items
        ) or "(Menu currently empty)"
    except Exception:
        menu_text = "(Menu unavailable right now)"

    # Active order context
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                items_str = ", ".join(f"{it.name} ×{it.quantity}" for it in (order.items or []))
                order_block = f"""
Current order context:
Order #{str(order.id)[-8:].upper()} — {order.status.upper()}
Total: R{order.total_amount:.2f}
Items: {items_str}
Address: {order.delivery_address or "Not specified"}
"""
        except Exception:
            pass

    return f"""You are KotaBot 🍔🤖 — the friendly AI helper for KotaBites, Johannesburg's favourite kota delivery service.

Your goals:
- Help customers track orders and explain statuses
- Recommend menu items based on preferences
- Accept suggestions, compliments or complaints
- Answer general questions about KotaBites

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
pending    → Waiting for payment confirmation
paid       → Payment received — kitchen starting soon
preparing  → Being cooked right now 🍳
ready      → Ready for delivery
delivered  → Delivered — enjoy your kota! 🎉
cancelled  → Order cancelled

{order_block}

Customer: {user.full_name} ({user.email})

Rules:
- Be warm, helpful, concise (max 3 short paragraphs)
- Use light South African slang sometimes: sharp, lekker, eish, ayt, jozi
- Never invent prices, items or order details
- If user mentions an order ID, explain its status if you have it
- If they give feedback/suggestion → thank them warmly & say it's noted
- Stay positive and professional
"""


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(req: ChatRequest, current_user: User = Depends(get_current_user)):
    """Non-streaming chat with KotaBot (Gemini)"""
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    try:
        # Convert messages to Gemini format
        gemini_messages = []
        for msg in req.messages:
            role = "user" if msg.role == "user" else "model"
            gemini_messages.append({"role": role, "parts": [msg.content]})

        response = genai.GenerativeModel(MODEL).generate_content(
            contents=gemini_messages,
            generation_config=types.GenerationConfig(
                temperature=0.7,
                max_output_tokens=600,
                candidate_count=1
            ),
            system_instruction=system_prompt
        )

        reply = response.text.strip()

        # Suggestion detection (keyword-based, cheap)
        last_user_msg = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
        suggestion_keywords = [
            "suggest", "would be nice", "wish", "feedback", "complaint", "improve",
            "add", "missing", "should have", "problem", "issue", "eish", "not happy"
        ]
        if any(kw in last_user_msg.lower() for kw in suggestion_keywords):
            await Suggestion(
                user_id=str(current_user.id),
                user_email=current_user.email,
                message=last_user_msg.strip(),
                category="general",
                sentiment="neutral",
                created_at=datetime.utcnow()
            ).insert()
            logger.info(f"Suggestion saved from user {current_user.email}")

        return {"reply": reply}

    except Exception as e:
        logger.exception("Gemini chat error")
        if "rate limit" in str(e).lower():
            raise HTTPException(429, "Rate limit reached — try again soon")
        raise HTTPException(500, "AI service error — please try again")


@router.post("/chat/stream")
async def ai_chat_stream(req: ChatRequest, current_user: User = Depends(get_current_user)):
    """Streaming chat with KotaBot (Gemini SSE)"""
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            model = genai.GenerativeModel(MODEL)
            # Gemini streaming
            response_stream = model.generate_content(
                contents=[{"role": "user" if m.role == "user" else "model", "parts": [m.content]} for m in req.messages],
                generation_config=types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=600
                ),
                system_instruction=system_prompt,
                stream=True
            )

            for chunk in response_stream:
                if chunk.text:
                    yield f"data: {json.dumps({'token': chunk.text})}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("Gemini stream error")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user)
):
    try:
        suggestion = Suggestion(
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=body.message.strip(),
            category=body.category.strip() if body.category else "general",
            sentiment="neutral",
            created_at=datetime.utcnow()
        )
        await suggestion.insert()
        logger.info(f"Suggestion saved from {current_user.email}")
        return {"msg": "Thank you! Your feedback has been received."}
    except Exception as e:
        logger.error(f"Suggestion save failed: {e}")
        raise HTTPException(500, "Failed to save feedback")


@router.get("/suggestions")
async def get_suggestions(current_user: User = Depends(get_current_user)):
    try:
        suggestions = await Suggestion.find_all().sort("-created_at").limit(100).to_list()
        summary = {"positive": 0, "neutral": 0, "negative": 0}
        for s in suggestions:
            summary[s.sentiment] = summary.get(s.sentiment, 0) + 1

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
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in suggestions
            ]
        }
    except Exception as e:
        logger.error(f"Failed to fetch suggestions: {e}")
        raise HTTPException(500, "Could not load suggestions")
