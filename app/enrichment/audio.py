from __future__ import annotations

import io
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_EXT_MAP: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/webm": "webm",
    "audio/wav": "wav",
}


async def transcribe_audio(data: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcribe audio bytes via OpenAI Whisper.

    Returns empty string if OPENAI_API_KEY is not configured or transcription
    fails.
    """
    if not settings.openai_api_key:
        logger.debug("OPENAI_API_KEY not set — skipping transcription")
        return ""

    try:
        from openai import AsyncOpenAI  # optional dependency
    except ImportError:
        logger.warning("openai package not installed — skipping transcription")
        return ""

    ext = _EXT_MAP.get(mime_type, "ogg")
    audio_buf = io.BytesIO(data)
    audio_buf.name = f"audio.{ext}"

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_buf,
            language="pt",
        )
        return transcript.text.strip()
    except Exception:
        logger.exception("Audio transcription failed")
        return ""
