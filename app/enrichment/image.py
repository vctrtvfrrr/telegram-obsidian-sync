from __future__ import annotations

import base64
import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)


async def describe_image(data: bytes, mime_type: str = "image/jpeg") -> str:
    """Analyze image bytes via Claude vision and return a concise description."""
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    b64 = base64.standard_b64encode(data).decode()

    # Normalize mime type — Telegram photos come as jpeg
    if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        mime_type = "image/jpeg"

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Descreva o conteúdo desta imagem de forma concisa e objetiva, "
                                "em português BR. Foque nos elementos principais e qualquer texto "
                                "visível. Máximo 3 frases."
                            ),
                        },
                    ],
                }
            ],
        )
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
    except Exception:
        logger.exception("Image description failed")

    return ""
