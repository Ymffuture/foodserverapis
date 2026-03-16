# routes/ai.py

import os
import json
import logging
import asyncio
import threading
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
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


# ─────────────────────────────────────────────────────────────
# Gemini Setup
# ─────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

MODEL = "gemini-2.5-flash"


# ─────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    order_id: Optional[str] = None


class SuggestionRequest(BaseModel):
    message: str = Field(..., min_length=5, max_length=2000)
    category: Optional[str] = Field(default="general", max_length=50)


# ─────────────────────────────────────────────────────────────
# Prompt Builder
# ─────────────────────────────────────────────────────────────

async def build_system_prompt(user: User, order_id: Optional[str] = None):

    # ── MENU ─────────────────────────────────
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

    # ── ACTIVE ORDER ─────────────────────────
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
Order #{str(order.id)[-8:].upper()}
Status: {str(order.status).upper()}
Total : R{order.total_amount:.2f}
Items : {items_str}
"""

        except Exception as e:
            logger.warning(f"Active order fetch failed: {e}")

    # ── ORDER HISTORY ─────────────────────────
    history_block = ""

    try:
        orders = await Order.find(Order.user_id == str(user.id)).to_list(length=10)

        if orders:

            orders.sort(key=lambda o: o.created_at, reverse=True)

            total_spent = sum(o.total_amount for o in orders)

            lines = []

            for o in orders:

                items = ", ".join(
                    f"{it.name}×{it.quantity}" for it in (o.items or [])
                )

                lines.append(
                    f"• #{str(o.id)[-8:].upper()} | R{o.total_amount:.2f} | {o.created_at.strftime('%d %b')} | {items}"
                )

            history_block = f"""
=== ORDER HISTORY ({len(orders)} orders | R{total_spent:.2f}) ===
{chr(10).join(lines)}
"""

        else:

            history_block = "=== ORDER HISTORY ===\nNo previous orders"

    except Exception as e:
        logger.warning(f"History fetch failed: {e}")

    # ── FINAL PROMPT ─────────────────────────
    return f"""
You are KotaBot 🍔🤖 — assistant for KotaBites in Johannesburg.

Be friendly and concise.

MENU
{menu_text}

{order_block}

{history_block}

CUSTOMER
Name: {user.full_name}
Email: {user.email}

Rules
- Max 5 short paragraphs
- Don't invent menu items
- If user gives feedback thank them
"""


# ─────────────────────────────────────────────────────────────
# Gemini Message Converter
# ─────────────────────────────────────────────────────────────

def _to_gemini_messages(messages: List[ChatMessage]):

    result = []

    for m in messages:

        role = "user" if m.role == "user" else "model"

        result.append({
            "role": role,
            "parts": [{"text": m.content}]
        })

    return result


# ─────────────────────────────────────────────────────────────
# Suggestion Auto Save
# ─────────────────────────────────────────────────────────────

SUGGESTION_KEYWORDS = [
    "suggest",
    "complaint",
    "feedback",
    "issue",
    "problem",
    "improve",
    "bad",
]


async def _maybe_save_suggestion(messages, user):

    last = next((m.content for m in reversed(messages) if m.role == "user"), "")

    if not any(k in last.lower() for k in SUGGESTION_KEYWORDS):
        return

    try:

        await Suggestion(
            user_id=str(user.id),
            user_email=user.email,
            message=last,
            category="general",
            sentiment="neutral",
            created_at=datetime.utcnow(),
        ).insert()

        logger.info(f"Suggestion saved from {user.email}")

    except Exception as e:
        logger.warning(f"Suggestion save failed: {e}")


# ─────────────────────────────────────────────────────────────
# NON STREAM CHAT
# ─────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user)
):

    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    try:

        model = genai.GenerativeModel(
            model_name=MODEL,
            system_instruction=system_prompt,
        )

        response = await asyncio.to_thread(
            model.generate_content,
            contents=_to_gemini_messages(req.messages),
        )

        reply = (response.text or "").strip()

        await _maybe_save_suggestion(req.messages, current_user)

        return {"reply": reply}

    except Exception as e:

        logger.exception("Gemini chat error")

        raise HTTPException(500, str(e))


# ─────────────────────────────────────────────────────────────
# STREAM CHAT (FIXED)
# ─────────────────────────────────────────────────────────────

@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user)
):

    if not GEMINI_API_KEY:
        raise HTTPException(503, "AI not configured")

    system_prompt = await build_system_prompt(current_user, req.order_id)

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    stop_event = threading.Event()

    loop = asyncio.get_event_loop()

    def run_stream():

        try:

            model = genai.GenerativeModel(
                model_name=MODEL,
                system_instruction=system_prompt,
            )

            stream = model.generate_content(
                contents=_to_gemini_messages(req.messages),
                stream=True,
            )

            for chunk in stream:

                if stop_event.is_set():
                    break

                text = getattr(chunk, "text", None)

                if not text:
                    continue

                loop.call_soon_threadsafe(queue.put_nowait, text)

        except Exception:

            logger.exception("Gemini stream error")

            loop.call_soon_threadsafe(queue.put_nowait, None)

        finally:

            loop.call_soon_threadsafe(queue.put_nowait, "<<DONE>>")

    async def event_generator():

        thread = threading.Thread(target=run_stream, daemon=True)

        thread.start()

        while True:

            if await request.is_disconnected():

                stop_event.set()

                break

            token = await queue.get()

            if token == "<<DONE>>":

                yield "data: [DONE]\n\n"

                break

            if token is None:

                yield "data: {\"error\":\"stream failed\"}\n\n"

                break

            yield f"data: {json.dumps({'token': token})}\n\n"

        stop_event.set()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ─────────────────────────────────────────────────────────────
# DIRECT SUGGESTION
# ─────────────────────────────────────────────────────────────

@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user)
):

    try:

        await Suggestion(
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=body.message,
            category=body.category,
            sentiment="neutral",
            created_at=datetime.utcnow(),
        ).insert()

        return {"msg": "Feedback received"}

    except Exception as e:

        logger.error(e)

        raise HTTPException(500, "Failed to save feedback")


# ─────────────────────────────────────────────────────────────
# GET ALL SUGGESTIONS
# ─────────────────────────────────────────────────────────────

@router.get("/suggestions")
async def get_suggestions():

    try:

        suggestions = await Suggestion.find_all().to_list(length=500)

        return {

            "total": len(suggestions),

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

        logger.error(e)

        raise HTTPException(500, "Failed to load suggestions")
