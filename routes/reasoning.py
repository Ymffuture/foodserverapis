# routes/reasoning.py
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

# ── Client ────────────────────────────────────────────────────────────────────
_API_KEY = os.getenv("KIMI_API_KEY")
_MODEL   = "nvidia/nemotron-3-super-120b-a12b:free"

_client: Optional[AsyncOpenAI] = None
if _API_KEY:
    _client = AsyncOpenAI(
        api_key=_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://foodsorder.vercel.app",
            "X-Title": "KotaBites-Reasoning",
        },
    )
    logger.info("[reasoning] client ready")
else:
    logger.warning("[reasoning] KIMI_API_KEY not set — keyword fallback only")

# ── Keyword fallback ──────────────────────────────────────────────────────────
_FALLBACK = {
    "track":    ["Identifying order reference…", "Querying order records…", "Fetching delivery status…", "Formatting result…"],
    "cancel":   ["Parsing cancellation intent…", "Verifying order ID…", "Checking eligibility window…", "Preparing confirmation…"],
    "menu":     ["Scanning available items…", "Checking today's specials…", "Matching preferences…", "Curating recommendations…"],
    "feedback": ["Logging feedback context…", "Identifying relevant item…", "Preparing response…"],
    "default":  ["Reading your message…", "Analysing intent…", "Checking relevant info…", "Composing reply…"],
}

def _keyword_fallback(text: str) -> List[str]:
    t = text.lower()
    if any(k in t for k in ("track", "where", "status")) or ("order" in t and "cancel" not in t):
        return _FALLBACK["track"]
    if "cancel" in t:
        return _FALLBACK["cancel"]
    if any(k in t for k in ("menu", "suggest", "kota", "eat", "food")):
        return _FALLBACK["menu"]
    if any(k in t for k in ("feedback", "complain", "review")):
        return _FALLBACK["feedback"]
    return _FALLBACK["default"]

# ── JSON extractor — handles wrapped/unwrapped model output ──────────────────
def _extract_steps(raw: str) -> Optional[List[str]]:
    """Try every reasonable way to get a JSON string array out of raw LLM output."""
    # 1. Strip markdown fences
    cleaned = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).strip()

    # 2. Find first [...] block in the text
    match = re.search(r'\[.*?\]', cleaned, re.DOTALL)
    if not match:
        return None

    try:
        steps = json.loads(match.group())
        if isinstance(steps, list) and 2 <= len(steps) <= 6:
            # Ensure every item is a non-empty string
            steps = [str(s).strip() for s in steps if str(s).strip()]
            if len(steps) >= 2:
                return steps
    except (json.JSONDecodeError, ValueError):
        pass
    return None

# ── Prompt ────────────────────────────────────────────────────────────────────
_SYSTEM = (
    "You are a reasoning engine for KotaBot, a South African food-ordering chatbot.\n"
    "Output ONLY a JSON array of 3-5 short reasoning steps (no markdown, no explanation).\n"
    "Rules: each step ≤9 words ending with '…', specific to the message, "
    "active verbs (Checking…, Verifying…, Fetching…).\n"
    'Example: ["Checking order status in database…","Fetching delivery info…","Formatting reply…"]'
)

# ── Schemas ───────────────────────────────────────────────────────────────────
class ReasoningRequest(BaseModel):
    message: str

class ReasoningResponse(BaseModel):
    steps:  List[str]
    source: str   # "openrouter" | "fallback"

# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user=Depends(get_current_user),
):
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message cannot be empty")

    user_text = body.message.strip()[:500]

    if _client:
        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": f'User message: "{user_text}"\nOutput only the JSON array:'},
                ],
                temperature=0.1,     # low = more deterministic JSON
                max_tokens=150,
                extra_body={"thinking": {"type": "disabled"}},
            )

            raw   = (response.choices[0].message.content or "").strip()
            steps = _extract_steps(raw)

            if steps:
                logger.info(f"[reasoning] OK — {len(steps)} steps")
                return ReasoningResponse(steps=steps, source="openrouter")

            logger.warning(f"[reasoning] could not extract steps from: {raw[:80]!r}")

        except Exception as exc:
            logger.warning(f"[reasoning] failed ({exc.__class__.__name__}: {exc})")

    steps = _keyword_fallback(user_text)
    return ReasoningResponse(steps=steps, source="fallback")
