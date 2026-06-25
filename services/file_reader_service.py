# services/file_reader_service.py
"""
File / document reading for KotaBot's chat.

Lets a customer attach an image (food photo, payment screenshot, app
error screenshot) or a PDF/text file to their chat message, and have
KotaBot actually "see" what's in it.

Uses the SAME google-genai client / GEMINI_API_KEY already configured for
routes/reasoning.py and services/id_verification_service.py — no new
dependency, no OCR binary, no new env var to set up.

KotaBot's own chat model (OpenRouter Nemotron) is text-only, so this
module is a pre-processing step: Gemini 2.5 Flash reads the file and
returns a plain-text description, which the caller folds into the chat
message before it ever reaches OpenRouter.

Never raises — on any failure (no API key, bad response, network error,
unsupported file) it returns a FileReadResult with ok=False and a short,
user-safe reason, so the caller can degrade gracefully instead of
crashing the whole chat turn.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import google.genai as genai
import google.genai.types as genai_types

logger = logging.getLogger(__name__)

# ── Gemini client (singleton — mirrors id_verification_service.py) ────────
_GEMINI_KEY = os.getenv("GEMINI_API_KEY")
_client: Optional[genai.Client] = None

if _GEMINI_KEY:
    _client = genai.Client(api_key=_GEMINI_KEY)
    logger.info("[file_reader] Gemini 2.5 Flash client ready")
else:
    logger.warning("[file_reader] GEMINI_API_KEY not set — chat file reading disabled")

# ── Limits ──────────────────────────────────────────────────────────────────
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB — comfortable for phone photos/PDFs

ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp",
    "image/heic", "image/heif", "image/gif",
    "application/pdf",
    "text/plain", "text/csv",
}

_SYSTEM = (
    "You are KotaBot's file-reading tool for KotaBites, a kota (sandwich) "
    "delivery service in Johannesburg South, South Africa. A customer has "
    "attached a file to their chat message. Look at it carefully and "
    "describe, in plain text, everything relevant a support assistant "
    "would need to help them.\n\n"
    "Examples of what to extract depending on the file:\n"
    "- Payment screenshot/receipt → amount, date, reference number, status\n"
    "- Photo of food/packaging → condition, and any visible issue (missing "
    "item, wrong order, damage, spillage, etc.)\n"
    "- Screenshot of an app/error page → transcribe the visible text and "
    "describe the UI state\n"
    "- Any other document → summarise its content factually\n\n"
    "Be factual and concise (under 150 words). Do not invent details that "
    "aren't visible. If the file is unreadable, blurry, or irrelevant to "
    "food delivery support, say so plainly instead of guessing."
)

_GEN_CONFIG = genai_types.GenerateContentConfig(
    temperature=0.1,
    max_output_tokens=400,
    system_instruction=_SYSTEM,
)


@dataclass
class FileReadResult:
    ok: bool
    description: Optional[str]   # Gemini's plain-text read of the file
    reason: Optional[str]        # set when ok=False — safe to show the user


def _call_gemini_sync(file_bytes: bytes, mime_type: str, user_question: str) -> str:
    assert _client is not None
    file_part = genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    prompt = user_question.strip() or "Describe what's in this file for customer support purposes."
    response = _client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[file_part, prompt],
        config=_GEN_CONFIG,
    )
    return (response.text or "").strip()


async def read_attachment(
    file_bytes: bytes,
    mime_type: str,
    user_question: str = "",
) -> FileReadResult:
    """
    Reads an uploaded file with Gemini 2.5 Flash and returns a plain-text
    description suitable for folding into KotaBot's chat context.

    `user_question` is optional context about what the customer actually
    wants to know about the file (e.g. "is this a valid payment?") — pass
    their chat message text here so the extraction stays relevant.
    """
    if not _client:
        return FileReadResult(ok=False, description=None, reason="File reading isn't configured right now.")

    if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
        return FileReadResult(
            ok=False, description=None,
            reason=f"'{mime_type or 'unknown'}' files aren't supported — try an image (jpg/png/webp) or PDF.",
        )

    if len(file_bytes) > MAX_FILE_BYTES:
        return FileReadResult(
            ok=False, description=None,
            reason=f"That file is too large ({len(file_bytes) // 1024} KB) — "
                   f"please keep it under {MAX_FILE_BYTES // (1024 * 1024)} MB.",
        )

    try:
        description = await asyncio.to_thread(_call_gemini_sync, file_bytes, mime_type, user_question)
        if not description:
            return FileReadResult(ok=False, description=None, reason="Couldn't read anything useful from that file.")
        return FileReadResult(ok=True, description=description, reason=None)
    except Exception as exc:
        logger.warning(f"[file_reader] Gemini read failed ({exc.__class__.__name__}: {exc})")
        return FileReadResult(
            ok=False, description=None,
            reason="Had trouble reading that file just now — try again or describe it in words.",
        )
