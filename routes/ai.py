# routes/ai.py
import os
import json
import logging
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic, APIError, RateLimitError, AnthropicError

from dependencies import get_current_user
from models.user import User
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion

router = APIRouter(prefix="/ai", tags=["AI Assistant"])

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ── Client ────────────────────────────────────────────────────────────────

anthropic_client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Current best models (as of March 2026)
SONNET_MODEL = "claude-3-5-sonnet-20241022"
HAIKU_MODEL  = "claude-3-haiku-20240307"

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


# ── System Prompt ─────────────────────────────────────────────────────────

async def build_system_prompt(user: User, order_id: Optional[str] = None) -> str:
    # Menu (limited to avoid huge context)
    try:
        items = await MenuItem.find_all().limit(60).to_list()
        menu_text = "\n".join(
            f"• {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description[:100]}" if i.description else "")
            for i in items
        ) or "(Menu currently empty)"
    except Exception as e:
        logger.warning(f"Failed to load menu for prompt: {e}")
        menu_text = "(Menu unavailable right now)"

    # Active order context (only if valid)
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
            order_block = "\n(Note: Order details could not be loaded — please paste the order ID if needed.)"

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
    """Non-streaming chat with KotaBot"""
    if not anthropic_client.api_key:
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    try:
        response = await anthropic_client.messages.create(
            model=SONNET_MODEL,
            max_tokens=600,
            temperature=0.7,
            system=system_prompt,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
        )

        reply = response.content[0].text.strip()

        # Simple suggestion detection (no extra API call)
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

    except RateLimitError:
        raise HTTPException(429, "Rate limit reached — please try again in a minute")
    except APIError as e:
        logger.error(f"Anthropic API error: {e.status_code} - {e.message}")
        if e.status_code == 401:
            raise HTTPException(503, "AI service authentication failed")
        if e.status_code in (429, 503):
            raise HTTPException(503, "AI service temporarily unavailable")
        raise HTTPException(500, "AI service error")
    except Exception as e:
        logger.exception("Unexpected error in /chat")
        raise HTTPException(500, "Something went wrong — please try again")


@router.post("/chat/stream")
async def ai_chat_stream(req: ChatRequest, current_user: User = Depends(get_current_user)):
    """Streaming chat with KotaBot (SSE)"""
    if not anthropic_client.api_key:
        raise HTTPException(503, "AI service not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async with anthropic_client.messages.stream(
                model=SONNET_MODEL,
                max_tokens=600,
                temperature=0.7,
                system=system_prompt,
                messages=[{"role": m.role, "content": m.content} for m in req.messages],
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield f"data: {json.dumps({'token': text})}\n\n"

            yield "data: [DONE]\n\n"

        except RateLimitError:
            yield f"data: {json.dumps({'error': 'Rate limit reached'})}\n\n"
        except APIError as e:
            logger.error(f"Stream API error: {e}")
            yield f"data: {json.dumps({'error': 'AI service unavailable'})}\n\n"
        except Exception as e:
            logger.exception("Stream error")
            yield f"data: {json.dumps({'error': 'Unexpected error'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user)
):
    """Save feedback/suggestion directly"""
    try:
        suggestion = Suggestion(
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=body.message.strip(),
            category=body.category.strip() if body.category else "general",
            sentiment="neutral",  # can be improved later
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
    """Admin-only: list all suggestions (add role check in production)"""
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
