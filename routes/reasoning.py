# routers/reasoning.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
import os, re, json, logging

import google.generativeai as genai
from dependencies.auth import get_current_user   # ← your existing auth dep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["AI"])

# ── Configure Gemini once at import time ──────────────────────────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if _GEMINI_KEY:
    genai.configure(api_key=_GEMINI_KEY)
else:
    logger.warning("GEMINI_API_KEY not set — reasoning will use keyword fallback")

# ── Keyword fallback (mirrors frontend fallback, runs server-side) ─────────────
_FALLBACK: dict[str, List[str]] = {
    "track":    ["Identifying order reference…", "Querying order records…",
                 "Fetching current delivery status…", "Formatting result for you…"],
    "cancel":   ["Parsing cancellation intent…", "Verifying order ID…",
                 "Checking cancellation eligibility…", "Preparing confirmation prompt…"],
    "menu":     ["Scanning available items…", "Checking today's specials…",
                 "Matching your preferences…", "Curating recommendations…"],
    "feedback": ["Logging feedback context…", "Identifying relevant item…",
                 "Preparing response…"],
    "default":  ["Reading your message carefully…", "Analysing intent and context…",
                 "Checking relevant info…", "Composing reply…"],
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

# ── Gemini call ───────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are the internal reasoning engine for KotaBot, a South African \
food-ordering chatbot for KOTABITES.

Given the user message below, return ONLY a JSON array of 3–5 short \
reasoning steps that KotaBot would think through before replying.

Rules:
- Each step max 9 words, ending with "…"
- Specific to THIS message — no generic filler
- Active present-tense verbs: Checking…, Verifying…, Scanning…, Fetching…
- If message contains an order ID (24-char hex), reference it in one step
- Return ONLY the raw JSON array — no markdown, no explanation

Example:
["Identifying order ID in message…","Querying delivery records…",\
"Checking estimated arrival window…","Formatting status update…"]\
"""

def _call_gemini(user_message: str) -> List[str]:
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        generation_config=genai.GenerationConfig(
            temperature=0.2,
            max_output_tokens=200,
            response_mime_type="application/json",
            response_schema={
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
        ),
        system_instruction=_SYSTEM_PROMPT,
    )

    response = model.generate_content(user_message)
    raw      = response.text.strip()

    # Strip accidental markdown fences
    clean = re.sub(r"```json|```", "", raw, flags=re.IGNORECASE).strip()
    steps = json.loads(clean)

    if not isinstance(steps, list) or not (2 <= len(steps) <= 6):
        raise ValueError(f"Bad shape from Gemini: {steps}")

    return [str(s) for s in steps]

# ── Request / Response schemas ────────────────────────────────────────────────
class ReasoningRequest(BaseModel):
    message: str

class ReasoningResponse(BaseModel):
    steps:  List[str]
    source: str   # "gemini" | "fallback"  — useful for debugging

# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user = Depends(get_current_user),   # auth-protected
):
    if not body.message.strip():
        raise HTTPException(status_code=422, detail="message cannot be empty")

    # Hard cap — prevent abuse
    user_text = body.message.strip()[:500]

    # Try Gemini first
    if _GEMINI_KEY:
        try:
            steps = _call_gemini(user_text)
            logger.info(f"[reasoning] Gemini OK — {len(steps)} steps")
            return ReasoningResponse(steps=steps, source="gemini")
        except Exception as exc:
            logger.warning(f"[reasoning] Gemini failed: {exc} — using fallback")

    # Keyword fallback
    steps = _keyword_fallback(user_text)
    return ReasoningResponse(steps=steps, source="fallback")
