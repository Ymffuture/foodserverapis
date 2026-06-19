# services/id_verification_service.py
"""
ID number verification for driver signup documents.

Uses Gemini 2.5 Flash's vision capability — the SAME google-genai client/
GEMINI_API_KEY already configured for routes/reasoning.py — to read the
13-digit South African ID number printed on an uploaded ID document or
driver's license photo, and compares it to the ID number the applicant
typed into the signup form.

No new system dependency required (no Tesseract/OCR binary to install).
If GEMINI_API_KEY isn't set, checks are simply skipped (checked=False) and
the caller should fall back to manual admin review rather than blocking
signup.
"""
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import google.genai as genai
import google.genai.types as genai_types

logger = logging.getLogger(__name__)

# ── Gemini client (singleton — mirrors routes/reasoning.py) ────────────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_client: Optional[genai.Client] = None

if _GEMINI_KEY:
    _client = genai.Client(api_key=_GEMINI_KEY)
    logger.info("[id_verification] Gemini 2.5 Flash client ready")
else:
    logger.warning("[id_verification] GEMINI_API_KEY not set — document ID checks disabled")

_SYSTEM = (
    "You are an OCR extraction tool for South African identity documents and "
    "driver's licenses. Examine the image and locate the 13-digit South "
    "African ID number printed on it (format YYMMDDSSSSCAZ — exactly 13 "
    "digits, no letters). On an ID book/card/smart-card it's usually the "
    "largest prominent number. On a driver's license it's usually labelled "
    "'ID NO' or 'IDENTITY NUMBER'.\n\n"
    "Return ONLY a raw JSON object — no markdown, no explanation, nothing else:\n"
    '{"id_number": "1234567890123", "found": true}\n\n'
    "If no clear 13-digit ID number is visible (blurry, cropped, glare, "
    "wrong document type, etc.) return exactly:\n"
    '{"id_number": null, "found": false}'
)

_GEN_CONFIG = genai_types.GenerateContentConfig(
    temperature=0,
    max_output_tokens=100,
    response_mime_type="application/json",
    system_instruction=_SYSTEM,
)

_NON_DIGIT_RE = re.compile(r"\D")


def _normalize(num: Optional[str]) -> Optional[str]:
    """Strip everything but digits. Returns None if nothing usable remains."""
    if not num:
        return None
    digits = _NON_DIGIT_RE.sub("", str(num))
    return digits or None


@dataclass
class IdCheckResult:
    checked: bool             # True if the Gemini call ran without error
    found: bool                # True if a 13-digit number was located in the image
    extracted: Optional[str]   # the number Gemini read, if any
    matched: Optional[bool]    # True/False vs the expected number; None if not found/checked


def _call_gemini_sync(image_bytes: bytes, mime_type: str) -> dict:
    assert _client is not None
    image_part = genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    response = _client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[image_part, "Extract the South African ID number from this document."],
        config=_GEN_CONFIG,
    )
    raw = response.text.strip()
    clean = re.sub(r"```json|```", "", raw, flags=re.IGNORECASE).strip()
    return json.loads(clean)


async def verify_id_number_in_document(
    image_bytes: bytes,
    mime_type: str,
    expected_id_number: str,
) -> IdCheckResult:
    """
    Reads the ID number off a document image and compares it to
    `expected_id_number`.

    Never raises — on any failure (no API key, bad response, network error)
    it returns checked=False so callers can fall back to manual admin review
    instead of blocking a legitimate applicant because of a transient issue.
    """
    if not _client:
        return IdCheckResult(checked=False, found=False, extracted=None, matched=None)

    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"

    try:
        data = await asyncio.to_thread(_call_gemini_sync, image_bytes, mime_type)
        extracted = _normalize(data.get("id_number"))
        found = bool(data.get("found")) and extracted is not None and len(extracted) == 13

        if not found:
            return IdCheckResult(checked=True, found=False, extracted=None, matched=None)

        matched = extracted == _normalize(expected_id_number)
        return IdCheckResult(checked=True, found=True, extracted=extracted, matched=matched)

    except Exception as exc:
        logger.warning(f"[id_verification] check failed ({exc.__class__.__name__}: {exc})")
        return IdCheckResult(checked=False, found=False, extracted=None, matched=None)
