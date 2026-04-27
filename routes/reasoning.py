# routes/reasoning.py (ADVANCED)

import asyncio
import json
import logging
import os
import re
import hashlib
from typing import List, Optional

import google.genai as genai
import google.genai.types as genai_types
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from dependencies import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["AI"])


# ── Config ──────────────────────────────────────────────────────────────────
TIMEOUT_SECONDS = 3.0
CACHE_TTL = 60  # seconds (simple in-memory)


# ── Simple in-memory cache (upgrade to Redis later) ──────────────────────────
_cache: dict[str, tuple[float, List[str]]] = {}


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _get_cache(key: str) -> Optional[List[str]]:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if (asyncio.get_event_loop().time() - ts) > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return data


def _set_cache(key: str, data: List[str]):
    _cache[key] = (asyncio.get_event_loop().time(), data)


# ── Gemini client ────────────────────────────────────────────────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_client: Optional[genai.Client] = None

if _GEMINI_KEY:
    _client = genai.Client(api_key=_GEMINI_KEY)
    logger.info("[reasoning] Gemini client ready")
else:
    logger.warning("[reasoning] No GEMINI_API_KEY — fallback only")


# ── System prompt ────────────────────────────────────────────────────────────
_SYSTEM = """
You are the internal reasoning engine for KotaBot.

Return ONLY a JSON array of 3–5 reasoning steps.

Rules:
- Max 9 words per step
- Must end with "…"
- No generic steps
- If order ID exists, include it
- No explanations, no markdown
"""


_GEN_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.2,
    max_output_tokens=120,
    response_mime_type="application/json",
    response_schema=list[str],
    system_instruction=_SYSTEM,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_json(text: str) -> List[str]:
    """Strict JSON parse with repair fallback"""
    text = re.sub(r"```json|```", "", text, flags=re.IGNORECASE).strip()

    try:
        return json.loads(text)
    except Exception:
        # 🔥 recovery attempt
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _normalize_steps(steps: List[str]) -> List[str]:
    """Enforce output constraints"""
    clean = []

    for s in steps:
        if not isinstance(s, str):
            continue

        s = s.strip()

        # enforce ellipsis
        if not s.endswith("…"):
            s += "…"

        # enforce max words (9)
        words = s.split()[:9]
        s = " ".join(words)

        clean.append(s)

    return clean[:5]


# ── Gemini call ──────────────────────────────────────────────────────────────

def _call_gemini_sync(user_message: str) -> List[str]:
    response = _client.models.generate_content(
        model="gemini-2.5-flash",
        contents=user_message,
        config=_GEN_CONFIG,
    )

    raw = response.text or ""
    steps = _clean_json(raw)

    if not isinstance(steps, list):
        raise ValueError("Invalid AI response")

    return _normalize_steps(steps)


# ── Fallback ─────────────────────────────────────────────────────────────────

def _fallback(text: str) -> List[str]:
    return [
        "Reading your message carefully…",
        "Understanding your request intent…",
        "Checking relevant system data…",
        "Preparing helpful response…",
    ]


# ── Schemas ──────────────────────────────────────────────────────────────────

class ReasoningRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)


class ReasoningResponse(BaseModel):
    steps: List[str]
    source: str
    cached: bool = False


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user=Depends(get_current_user),
):
    user_text = body.message.strip()

    # 🔥 CACHE FIRST (huge performance win)
    key = _cache_key(user_text)
    cached = _get_cache(key)
    if cached:
        return ReasoningResponse(steps=cached, source="cache", cached=True)

    # 🔥 AI CALL WITH TIMEOUT
    if _client:
        try:
            steps = await asyncio.wait_for(
                asyncio.to_thread(_call_gemini_sync, user_text),
                timeout=TIMEOUT_SECONDS
            )

            _set_cache(key, steps)

            return ReasoningResponse(
                steps=steps,
                source="gemini",
                cached=False
            )

        except Exception as e:
            logger.warning(f"[reasoning] AI failed: {e}")

    # 🔥 FALLBACK (always safe)
    steps = _fallback(user_text)
    return ReasoningResponse(
        steps=steps,
        source="fallback",
        cached=False
    )
