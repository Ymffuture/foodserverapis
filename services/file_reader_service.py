# services/file_reader_service.py
"""
File / document / voice-note reading for KotaBot's chat.

Lets a customer attach an image (food photo, payment screenshot, app
error screenshot), a PDF/text file, or a recorded voice note to their
chat message, and have KotaBot actually "see" or "hear" what's in it.

Uses the SAME google-genai client / GEMINI_API_KEY already configured for
routes/reasoning.py and services/id_verification_service.py — no new
dependency, no OCR/STT binary, no new env var to set up. Gemini 2.5 Flash
natively understands images, PDFs, and audio in the same call.

KotaBot's own chat model (OpenRouter Nemotron) is text-only, so this
module is a pre-processing step:
- Images/PDFs/text  → Gemini returns a plain-text DESCRIPTION, which the
  caller folds into the chat message as hidden context before it ever
  reaches OpenRouter.
- Audio (voice notes) → Gemini returns a verbatim TRANSCRIPT instead of a
  description, since the caller drops a voice note straight into the
  visible chat input for the customer to review/edit before sending.

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
MAX_FILE_BYTES = 8 * 1024 * 1024  # 8 MB — comfortable for phone photos/PDFs/short voice notes

DOCUMENT_MIME_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp",
    "image/heic", "image/heif", "image/gif",
    "application/pdf",
    "text/plain", "text/csv",
}

# Covers what browser MediaRecorder implementations actually produce
# (Chrome/Edge/Firefox → audio/webm, Safari/iOS → audio/mp4) plus common
# pre-recorded formats, in case a user attaches an existing voice memo.
AUDIO_MIME_TYPES = {
    "audio/webm", "audio/ogg", "audio/mp4", "audio/m4a",
    "audio/mp3", "audio/mpeg", "audio/wav", "audio/x-wav",
    "audio/aac", "audio/3gpp", "audio/3gpp2",
}

ALLOWED_MIME_TYPES = DOCUMENT_MIME_TYPES | AUDIO_MIME_TYPES

_SYSTEM = (
    "You are KotaBot's file-reading tool for KotaBites, a kota (sandwich) "
    "delivery service in Johannesburg South, South Africa. A customer has "
    "attached a file to their chat message.\n\n"
    "If the file is AUDIO (a voice note): transcribe exactly what the "
    "speaker says, verbatim, in their own words — fix only obvious "
    "punctuation/capitalisation. Do NOT summarise, describe tone, or add "
    "commentary; return ONLY the spoken words as plain text, since this "
    "transcript gets dropped straight into the customer's chat input for "
    "them to review and send. If the audio is silent, unclear, or has no "
    "speech, say so plainly in one short sentence instead of guessing.\n\n"
    "Otherwise (image, PDF, or text document): look at it carefully and "
    "describe, in plain text, everything relevant a support assistant "
    "would need to help the customer. Examples:\n"
    "- Payment screenshot/receipt → amount, date, reference number, status\n"
    "- Photo of food/packaging → condition, and any visible issue (missing "
    "item, wrong order, damage, spillage, etc.)\n"
    "- Screenshot of an app/error page → transcribe the visible text and "
    "describe the UI state\n"
    "- Any other document → summarise its content factually\n"
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
    description: Optional[str]   # Gemini's transcript (audio) or description (everything else)
    reason: Optional[str]        # set when ok=False — safe to show the user


def _normalize_mime(mime_type: str) -> str:
    """Strips codec params browsers append (e.g. 'audio/webm;codecs=opus' → 'audio/webm')."""
    return (mime_type or "").split(";")[0].strip().lower()


def _call_gemini_sync(file_bytes: bytes, mime_type: str, user_question: str) -> str:
    assert _client is not None
    file_part = genai_types.Part.from_bytes(data=file_bytes, mime_type=mime_type)
    if user_question.strip():
        prompt = user_question.strip()
    elif mime_type in AUDIO_MIME_TYPES:
        prompt = "Transcribe exactly what is said in this voice note."
    else:
        prompt = "Describe what's in this file for customer support purposes."
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
    Reads an uploaded file (or transcribes a voice note) with Gemini 2.5
    Flash. `mime_type` may include codec params (e.g. from MediaRecorder)
    — it's normalized internally, so callers can pass it through as-is.

    `user_question` is optional context about what the customer actually
    wants to know about the file (e.g. "is this a valid payment?") — pass
    their chat message text here so the extraction stays relevant. Leave
    blank for voice notes; transcription mode ignores it anyway.
    """
    if not _client:
        return FileReadResult(ok=False, description=None, reason="File reading isn't configured right now.")

    mime_type = _normalize_mime(mime_type)
    if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
        return FileReadResult(
            ok=False, description=None,
            reason=f"'{mime_type or 'unknown'}' files aren't supported — try an image, PDF, or voice note.",
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
