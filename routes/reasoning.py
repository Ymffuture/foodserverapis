# routes/reasoning.py
"""
/ai/reasoning — generates context-aware reasoning steps for KotaBot's
thinking block UI.

Uses OpenRouter (KIMI_API_KEY_2) — same setup as ai.py but a separate
API key so reasoning quota is isolated from the main chat quota.
Falls back to keyword buckets if the key is missing or the call fails.
"""
import json
import logging
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

from dependencies import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["AI"])

# ── OpenRouter client (separate key from /ai/chat) ────────────────────────────
_API_KEY = os.getenv("KIMI_API_KEY_2")
_MODEL   = "nvidia/nemotron-3-super-120b-a12b:free"   # fast, free — good for short structured output

_client: Optional[AsyncOpenAI] = None

if _API_KEY:
    _client = AsyncOpenAI(
        api_key=_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://foodsorder.vercel.app",
            "X-Title":      "KotaBites-Reasoning",
        },
    )
    logger.info("[reasoning] OpenRouter client ready (KIMI_API_KEY_2)")
else:
    logger.warning("[reasoning] KIMI_API_KEY_2 not set — keyword fallback only")

# ── Keyword fallback ──────────────────────────────────────────────────────────
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


# ── Prompt ────────────────────────────────────────────────────────────────────
_SYSTEM = (
    "You are the internal reasoning engine for KotaBot, a South African "
    "food-ordering chatbot for KOTABITES.\n\n"
    "Given the user message, return ONLY a raw JSON array of 3 to 5 short "
    "reasoning steps that KotaBot would think through before replying.\n\n"
    "Rules:\n"
    "- Each step max 9 words, ending with '…'\n"
    "- Must be specific to THIS message — no generic filler\n"
    "- Active present-tense verbs: Checking…, Verifying…, Scanning…, Fetching…\n"
    "- If message has a 24-char hex order ID, reference it in one step\n"
    "- Return ONLY the JSON array — no markdown, no explanation, nothing else\n\n"
    'Example: ["Identifying order ID in message…","Querying delivery records…",'
    '"Checking estimated arrival window…","Formatting status update…"]'
)


# ── Schemas ───────────────────────────────────────────────────────────────────
class ReasoningRequest(BaseModel):
    message: str


class ReasoningResponse(BaseModel):
    steps:  List[str]
    source: str    # "openrouter" | "fallback"


# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user=Depends(get_current_user),
):
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message cannot be empty")

    user_text = body.message.strip()[:500]   # hard cap

    if _client:
        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": user_text},
                ],
                temperature=0.2,
                max_tokens=180,
                extra_body={"thinking": {"type": "disabled"}},
            )

            raw   = (response.choices[0].message.content or "").strip()
            clean = re.sub(r"```json|```", "", raw, flags=re.IGNORECASE).strip()
            steps = json.loads(clean)

            if isinstance(steps, list) and 2 <= len(steps) <= 6:
                logger.info(f"[reasoning] OpenRouter OK — {len(steps)} steps")
                return ReasoningResponse(
                    steps=[str(s) for s in steps],
                    source="openrouter",
                )

            logger.warning(f"[reasoning] bad shape: {steps!r} — using fallback")

        except Exception as exc:
            logger.warning(f"[reasoning] OpenRouter failed ({exc.__class__.__name__}: {exc}) — using fallback")

    steps = _keyword_fallback(user_text)
    logger.info(f"[reasoning] keyword fallback — {len(steps)} steps")
    return ReasoningResponse(steps=steps, source="fallback")
