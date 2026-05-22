from __future__ import annotations

import logging
import re

import anthropic
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


async def fetch_link_content(url: str) -> str:
    """Fetch a URL and return a Portuguese summary of its main content.

    Returns empty string if the page is inaccessible or no useful content
    can be extracted.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception:
        logger.warning("Could not fetch URL: %s", url)
        return ""

    # Strip HTML tags and collapse whitespace
    text = _TAG_RE.sub(" ", html)
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) < 50:
        return ""

    # Cap input to avoid large token costs
    text = text[:8000]

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Extraia e resuma o conteúdo principal desta página web em português BR, "
                        f"em 2 a 4 frases. Ignore menus, anúncios e rodapé.\n\n"
                        f"URL: {url}\n\nConteúdo extraído:\n{text}"
                    ),
                }
            ],
        )
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
    except Exception:
        logger.exception("Link summarization failed for %s", url)

    return ""
