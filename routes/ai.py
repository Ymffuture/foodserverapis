# routes/ai.py
import os
import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from models.order import Order
from models.menu import MenuItem
from models.suggestion import Suggestion
from dependencies import get_current_user
from models.user import User
import json

router = APIRouter()

# Initialise Anthropic client — reads ANTHROPIC_API_KEY from env
_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Schemas ──────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str       # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    order_id: Optional[str] = None   # pre-load order context if user is on /order/:id


class SuggestionRequest(BaseModel):
    message: str
    category: Optional[str] = "general"


# ── Helpers ──────────────────────────────────────────────────────────────────

STATUS_DESCRIPTIONS = {
    "pending":   "Your order has been placed and is waiting for payment confirmation.",
    "paid":      "Payment received! Our kitchen will start preparing your order shortly.",
    "preparing": "Our chefs are busy making your food right now 🍳",
    "ready":     "Your order is ready and on its way to you!",
    "delivered": "Your order has been delivered. Enjoy your meal! 🎉",
    "cancelled": "This order was cancelled. You can place a new one from the menu.",
}

async def _build_system_prompt(
    user: User,
    order_id: Optional[str] = None,
) -> str:
    # ── Menu context ──
    try:
        items = await MenuItem.find_all().to_list(length=200)
        menu_lines = [
            f"  • {i.name} — R{i.price:.2f} [{i.category}]"
            + (f": {i.description}" if i.description else "")
            for i in items
        ]
        menu_text = "\n".join(menu_lines) if menu_lines else "  (Menu currently unavailable)"
    except Exception:
        menu_text = "  (Menu could not be loaded)"

    # ── Order context ──
    order_block = ""
    if order_id:
        try:
            order = await Order.get(order_id)
            if order and order.user_id == str(user.id):
                item_lines = ", ".join(
                    f"{it.name} ×{it.quantity}" for it in (order.items or [])
                )
                status_desc = STATUS_DESCRIPTIONS.get(str(order.status), "")
                order_block = f"""
Active order the customer is asking about:
  • Order ID : {order.id} (short: #{str(order.id)[-8:].upper()})
  • Status   : {order.status}  — {status_desc}
  • Total    : R{order.total_amount:.2f}
  • Payment  : {order.payment_method or "paystack"}
  • Address  : {order.delivery_address}
  • Items    : {item_lines}
"""
        except Exception:
            order_block = "\n  (Order could not be retrieved — the ID may be invalid.)\n"

    # ── Recent order history ──
    try:
        recent = await Order.find(Order.user_id == str(user.id)).limit(5).to_list()
        if recent:
            hist_lines = [
                f"  • #{str(o.id)[-8:].upper()} — {o.status} — R{o.total_amount:.2f} — "
                + ", ".join(f"{it.name} ×{it.quantity}" for it in (o.items or []))
                for o in recent
            ]
            history_block = "Customer's recent orders:\n" + "\n".join(hist_lines)
        else:
            history_block = "Customer has no previous orders yet."
    except Exception:
        history_block = ""

    return f"""You are KotaBot 🤖🍔, the friendly AI assistant for KotaBites — a fast kota sandwich delivery service in Johannesburg, South Africa.

You help customers:
1. Track their orders and understand what each status means.
2. Discover menu items and get personalised recommendations.
3. Submit suggestions or complaints (you extract and save them).
4. Answer general questions about KotaBites.

=== CURRENT MENU ===
{menu_text}

=== ORDER STATUSES ===
  pending   → Placed, awaiting payment
  paid      → Payment confirmed, kitchen starting soon
  preparing → Being cooked right now
  ready     → Ready for delivery
  delivered → Delivered ✓
  cancelled → Cancelled

{order_block}
{history_block}

=== CUSTOMER INFO ===
  Name  : {user.full_name}
  Email : {user.email}

=== BEHAVIOUR RULES ===
- Be warm, helpful and concise (max 3 short paragraphs per reply).
- Use South African slang occasionally: "sharp", "lekker", "ayt", "eish", "jozi".
- When a customer pastes an order ID (24 hex chars or 8-char short code), tell them the status.
- If a customer gives feedback or a suggestion, acknowledge it warmly and tell them you've noted it.
- Never make up prices or menu items not listed above.
- If asked to track an order and you don't have its details above, ask the customer to paste the Order ID from their confirmation.
"""


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/chat")
async def ai_chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Non-streaming chat with KotaBot."""
    system = await _build_system_prompt(current_user, req.order_id)

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system,
        messages=[{"role": m.role, "content": m.content} for m in req.messages],
    )

    reply = response.content[0].text

    # ── Auto-save if the message looks like a suggestion / complaint ──
    last_user_msg = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )
    suggestion_keywords = [
        "suggest", "would be nice", "wish", "feedback", "complaint",
        "improve", "add", "missing", "should have", "problem", "issue",
        "eish", "not happy", "disappointed",
    ]
    if any(kw in last_user_msg.lower() for kw in suggestion_keywords):
        # Detect category + sentiment via a quick cheap call
        try:
            meta = _client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{
                    "role": "user",
                    "content": (
                        f'Classify this feedback JSON only, no extra text: '
                        f'{{"category": one of food/service/app/general, '
                        f'"sentiment": one of positive/neutral/negative}} '
                        f'Feedback: "{last_user_msg}"'
                    ),
                }],
            )
            meta_json = json.loads(meta.content[0].text)
            category  = meta_json.get("category", "general")
            sentiment = meta_json.get("sentiment", "neutral")
        except Exception:
            category, sentiment = "general", "neutral"

        suggestion = Suggestion(
            user_id=str(current_user.id),
            user_email=current_user.email,
            message=last_user_msg,
            category=category,
            sentiment=sentiment,
        )
        await suggestion.insert()

    return {"reply": reply}


@router.post("/chat/stream")
async def ai_chat_stream(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Server-sent events streaming version of KotaBot chat."""
    system = await _build_system_prompt(current_user, req.order_id)

    def generate():
        with _client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
        ) as stream:
            for text in stream.text_stream:
                # SSE format
                yield f"data: {json.dumps({'token': text})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/suggestion", status_code=201)
async def save_suggestion(
    body: SuggestionRequest,
    current_user: User = Depends(get_current_user),
):
    """Directly save a suggestion (used by dedicated feedback form)."""
    suggestion = Suggestion(
        user_id=str(current_user.id),
        user_email=current_user.email,
        message=body.message,
        category=body.category,
    )
    await suggestion.insert()
    return {"msg": "Suggestion saved. Thank you for the feedback!"}


@router.get("/suggestions")
async def get_suggestions(current_user: User = Depends(get_current_user)):
    """Admin: list all suggestions with sentiment breakdown."""
    suggestions = await Suggestion.find_all().to_list()
    summary = {"positive": 0, "neutral": 0, "negative": 0}
    for s in suggestions:
        if s.sentiment in summary:
            summary[s.sentiment] += 1

    return {
        "total": len(suggestions),
        "sentiment_summary": summary,
        "items": [
            {
                "id":        str(s.id),
                "email":     s.user_email,
                "message":   s.message,
                "category":  s.category,
                "sentiment": s.sentiment,
                "created_at": s.created_at,
            }
            for s in suggestions
        ],
    }
