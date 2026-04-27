# routes/reasoning.py
"""
/ai/reasoning  — Gemini 2.5 Flash generates context-aware reasoning steps
                 for KotaBot's thinking block UI.

Uses the new `google-genai` SDK (replaces deprecated `google-generativeai`).
The blocking SDK call runs in a thread pool via asyncio.to_thread so it
never blocks FastAPI's async event loop.
"""
import asyncio
import json
import logging
import os
import re
from typing import List

import google.genai as genai
import google.genai.types as genai_types
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import get_current_user   # flat import — dependencies.py

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["AI"])

# ── Gemini client (singleton, created once at startup) ────────────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_client: genai.Client | None = None

if _GEMINI_KEY:
    _client = genai.Client(api_key=_GEMINI_KEY)
    logger.info("[reasoning] Gemini 2.5 Flash client ready")
else:
    logger.warning("[reasoning] GEMINI_API_KEY not set — keyword fallback only")

# ── Keyword fallback (used when Gemini is unavailable or fails) ───────────────
_FALLBACK: dict[str, List[str]] = {
    "track": [
        "Identifying order reference in message…",
        "Querying order records in database…",
        "Fetching current delivery status…",
        "Formatting result for you…",
    ],
    "cancel": [
        "Parsing cancellation intent…",
        "Verifying order ID exists…",
        "Checking cancellation eligibility…",
        "Preparing confirmation prompt…",
    ],
    "menu": [
        "Scanning available menu items…",
        "Checking today's specials…",
        "Matching your preferences…",
        "Curating best recommendations…",
    ],
    "feedback": [
        "Logging your feedback context…",
        "Identifying the relevant item…",
        "Preparing response…",
    ],
    "default": [
        "Reading your message carefully…",
        "Analysing intent and context…",
        "Checking relevant information…",
        "Composing the best reply…",
    ],
}


def _keyword_fallback(text: str) -> List[str]:
    t = text.lower()
    if any(k in t for k in ("track", "where", "status")) or \
       ("order" in t and "cancel" not in t):
        return _FALLBACK["track"]
    if "cancel" in t:
        return _FALLBACK["cancel"]
    if any(k in t for k in ("menu", "suggest", "kota", "eat", "food")):
        return _FALLBACK["menu"]
    if any(k in t for k in ("feedback", "complain", "review")):
        return _FALLBACK["feedback"]
    return _FALLBACK["default"]


# ── System instruction ────────────────────────────────────────────────────────
_SYSTEM = (
    "You are the internal reasoning engine for KotaBot, a South African "
    "food-ordering chatbot for KOTABITES.\n\n"
    "Given a user message, return ONLY a JSON array of 3 to 5 short reasoning "
    "steps KotaBot would think through before replying.\n\n"
    "Rules:\n"
    "- Each step max 9 words, ending with '…'\n"
    "- Specific to THIS exact message — no generic filler\n"
    "- Active present-tense verbs: Checking…, Verifying…, Scanning…, Fetching…\n"
    "- If message contains a 24-char hex order ID, reference it in one step\n"
    "- Return ONLY a raw JSON array — no markdown, no explanation, nothing else\n\n"
    'Example: ["Identifying order ID in message…","Querying delivery records…",'
    '"Checking estimated arrival window…","Formatting status update…"]'
)

# ── Gemini call config ────────────────────────────────────────────────────────
_GEN_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.2,
    max_output_tokens=200,
    response_mime_type="application/json",
    response_schema=list[str],          # new SDK accepts Python type hints directly
    system_instruction=_SYSTEM,
)


# ── Sync call (runs in thread pool, never blocks event loop) ──────────────────
def _call_gemini_sync(user_message: str) -> List[str]:
    assert _client is not None

    response = _client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_message,
        config=_GEN_CONFIG,
    )

    raw   = response.text.strip()
    clean = re.sub(r"```json|```", "", raw, flags=re.IGNORECASE).strip()
    steps = json.loads(clean)

    if not isinstance(steps, list) or not (2 <= len(steps) <= 6):
        raise ValueError(f"Unexpected shape from Gemini: {steps!r}")

    return [str(s) for s in steps]


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class ReasoningRequest(BaseModel):
    message: str


class ReasoningResponse(BaseModel):
    steps:  List[str]
    source: str   # "gemini" | "fallback"


# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user=Depends(get_current_user),
):
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message cannot be empty")

    user_text = body.message.strip()[:500]   # hard cap — abuse prevention

    if _client:
        try:
            steps = await asyncio.to_thread(_call_gemini_sync, user_text)
            logger.info(f"[reasoning] Gemini 2.5 Flash OK — {len(steps)} steps for: {user_text[:60]}")
            return ReasoningResponse(steps=steps, source="gemini")
        except Exception as exc:
            logger.warning(f"[reasoning] Gemini failed ({exc.__class__.__name__}: {exc}) — using fallback")

    steps = _keyword_fallback(user_text)
    logger.info(f"[reasoning] keyword fallback — {len(steps)} steps")
    return ReasoningResponse(steps=steps, source="fallback")
