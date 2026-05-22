from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from typing import Any

import pytz

from app.config import settings
from app.session import PendingEnrichment, Session, SessionManager
from app.vault.gitea import gitea

logger = logging.getLogger(__name__)

_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
}

_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _local_now() -> datetime:
    tz = pytz.timezone(settings.note_timezone)
    return datetime.now(tz)


def _media_type_label(update: Any) -> tuple[str, str | None]:
    """
    Returns (media_type_label, mime_type) based on the update.
    media_type_label is one of: photo, video, document, voice, audio
    """
    msg = update.effective_message
    if msg.photo:
        return "photo", "image/jpeg"
    if msg.video:
        return "video", msg.video.mime_type
    if msg.document:
        return "document", msg.document.mime_type
    if msg.voice:
        return "voice", msg.voice.mime_type or "audio/ogg"
    if msg.audio:
        return "audio", msg.audio.mime_type
    return "file", None


def _resolve_extension(mime_type: str | None, filename: str | None) -> str:
    """Determine a file extension from MIME type or filename."""
    if mime_type and mime_type in _MIME_TO_EXT:
        return _MIME_TO_EXT[mime_type]
    if filename:
        _, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
        if ext:
            return ext.lower()
    if mime_type:
        ext = mimetypes.guess_extension(mime_type)
        if ext:
            return ext.lstrip(".")
    return "bin"


async def handle_media(
    update: Any,
    session: Session,
    session_manager: SessionManager,
    bot: Any,
) -> tuple[str, PendingEnrichment | None]:
    """Download media from Telegram, upload to Gitea as an asset.

    Returns (obsidian_ref, pending_enrichment).
    pending_enrichment is non-None when the media type supports enrichment and
    the user should be asked before processing (image without caption, voice).
    """
    msg = update.effective_message
    media_label, mime_type = _media_type_label(update)
    filename_hint: str | None = None
    enrichment: PendingEnrichment | None = None

    # Determine file_id and optional filename hint
    if msg.photo:
        file_id = msg.photo[-1].file_id  # highest resolution
    elif msg.video:
        file_id = msg.video.file_id
        filename_hint = msg.video.file_name
    elif msg.document:
        file_id = msg.document.file_id
        filename_hint = msg.document.file_name
        if not mime_type and filename_hint:
            mime_type, _ = mimetypes.guess_type(filename_hint)
    elif msg.voice:
        file_id = msg.voice.file_id
    elif msg.audio:
        file_id = msg.audio.file_id
        filename_hint = msg.audio.file_name
    else:
        return "", None

    ext = _resolve_extension(mime_type, filename_hint)
    now = _local_now()
    asset_filename = f"{now.strftime('%Y-%m-%d_%H%M%S')}_{media_label}.{ext}"
    asset_vault_path = f"Inbox/assets/{asset_filename}"

    # Download from Telegram
    tg_file = await bot.get_file(file_id)
    data: bytearray = await tg_file.download_as_bytearray()

    # Upload to Gitea
    try:
        await gitea.create_file_bytes(
            asset_vault_path,
            bytes(data),
            f"media: upload {asset_filename}",
        )
    except Exception as exc:
        logger.exception("Failed to upload media to Gitea: %s", exc)
        raise

    # Build Obsidian reference
    is_image = mime_type in _IMAGE_MIMES if mime_type else media_label == "photo"
    obsidian_ref = f"![[assets/{asset_filename}]]" if is_image else f"[[assets/{asset_filename}]]"

    # Determine if enrichment should be offered
    has_caption = bool(msg.caption)
    if msg.photo and not has_caption:
        enrichment = PendingEnrichment(
            chat_id=session.chat_id,
            enrichment_type="image",
            file_id=file_id,
            url=None,
            mime_type=mime_type or "image/jpeg",
        )
    elif msg.voice:
        enrichment = PendingEnrichment(
            chat_id=session.chat_id,
            enrichment_type="voice",
            file_id=file_id,
            url=None,
            mime_type=mime_type or "audio/ogg",
        )
    # audio (music files) and video/document: no enrichment offered

    return obsidian_ref, enrichment
