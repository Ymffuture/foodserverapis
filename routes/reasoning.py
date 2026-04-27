# routes/reasoning.py

"""
/ai/reasoning — Kimi (OpenRouter) generates structured reasoning steps
for KotaBot thinking UI.

Production features:
- Async AI calls (no blocking)
- Timeout protection
- In-memory caching (upgrade to Redis later)
- JSON repair + strict validation
- Order ID extraction
- Fallback resilience
"""

import asyncio
import json
import logging
import os
import re
import hashlib
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

from dependencies import get_current_user


# ── Setup ───────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["AI"])


# ── Config ──────────────────────────────────────────────────────────────────

KIMI_API_KEY = os.getenv("KIMI_API_KEY")
MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

TIMEOUT_SECONDS = 3.0
CACHE_TTL = 60  # seconds


# ── OpenRouter / Kimi Client ────────────────────────────────────────────────

client: Optional[AsyncOpenAI] = None

if KIMI_API_KEY:
    client = AsyncOpenAI(
        api_key=KIMI_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://foodsorder.vercel.app",
            "X-Title": "KotaBites",
        },
    )
    logger.info("[reasoning] Kimi client ready")
else:
    logger.warning("[reasoning] No KIMI_API_KEY — fallback only")


# ── Cache (simple in-memory) ────────────────────────────────────────────────

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


# ── AI Prompt ───────────────────────────────────────────────────────────────

SYSTEM = """
You are KotaBot's internal reasoning engine.

Return ONLY a JSON array of 3–5 steps.

Rules:
- Max 9 words per step
- Each must end with "…"
- No explanations, no markdown
- Must be specific to the message
- If order ID exists, include it
"""


# ── Helpers ─────────────────────────────────────────────────────────────────

ORDER_REGEX = r"(ORD-\d+|[a-f0-9]{24})"


def extract_order_id(text: str) -> Optional[str]:
    match = re.search(ORDER_REGEX, text)
    return match.group(0) if match else None


def _process_ai_output(text: str) -> List[str]:
    """Parse + repair + normalize AI output"""

    text = re.sub(r"```json|```", "", text, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
        else:
            raise ValueError("Invalid JSON from AI")

    if not isinstance(data, list):
        raise ValueError("AI response not list")

    clean = []

    for s in data:
        if not isinstance(s, str):
            continue

        s = s.strip()

        # enforce ellipsis
        if not s.endswith("…"):
            s += "…"

        # enforce max 9 words
        s = " ".join(s.split()[:9])

        clean.append(s)

    return clean[:5]


def _fallback(_: str) -> List[str]:
    return [
        "Reading your message carefully…",
        "Understanding your request intent…",
        "Checking relevant system data…",
        "Preparing helpful response…",
    ]


# ── AI Call ─────────────────────────────────────────────────────────────────

async def _call_kimi(user_message: str) -> List[str]:
    response = await client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        max_tokens=120,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_message},
        ],
    )

    raw = response.choices[0].message.content or ""
    return _process_ai_output(raw)


# ── Schemas ─────────────────────────────────────────────────────────────────

class ReasoningRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)


class ReasoningResponse(BaseModel):
    steps: List[str]
    source: str   # "kimi" | "fallback" | "cache"
    cached: bool = False


# ── Endpoint ────────────────────────────────────────────────────────────────

@router.post("/reasoning", response_model=ReasoningResponse)
async def get_reasoning(
    body: ReasoningRequest,
    current_user=Depends(get_current_user),
):
    user_text = body.message.strip()

    if not user_text:
        raise HTTPException(status_code=422, detail="message cannot be empty")

    # ── Cache first (fast path) ──────────────────────────────────────────────
    key = _cache_key(user_text)
    cached = _get_cache(key)
    if cached:
        return ReasoningResponse(
            steps=cached,
            source="cache",
            cached=True
        )

    # ── Inject order context (smart reasoning) ───────────────────────────────
    order_id = extract_order_id(user_text)
    if order_id:
        user_text += f"\nOrder ID: {order_id}"

    # ── AI call with timeout ─────────────────────────────────────────────────
    if client:
        try:
            steps = await asyncio.wait_for(
                _call_kimi(user_text),
                timeout=TIMEOUT_SECONDS
            )

            _set_cache(key, steps)

            logger.info(f"[reasoning] Kimi OK ({len(steps)} steps)")
            return ReasoningResponse(
                steps=steps,
                source="kimi",
                cached=False
            )

        except Exception as e:
            logger.warning(f"[reasoning] Kimi failed: {e}")

    # ── Fallback (always safe) ───────────────────────────────────────────────
    steps = _fallback(user_text)

    return ReasoningResponse(
        steps=steps,
        source="fallback",
        cached=False
    )
