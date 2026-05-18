from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, Response
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.assistant import assistant
from app.config import settings
from app.handlers.media import handle_media
from app.session import Session, session_manager
from app.vault.gitea import gitea
from app.vault.writer import inbox_path

logger = logging.getLogger(__name__)

# Module-level reference to the PTB Application (set during lifespan startup)
_ptb_app: Application | None = None


# ---------------------------------------------------------------------------
# Whitelist guard
# ---------------------------------------------------------------------------


def _is_allowed(chat_id: int) -> bool:
    allowed = settings.allowed_chat_ids_list
    if not allowed:
        return True
    return chat_id in allowed


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


async def _get_or_create_session(chat_id: int) -> Session:
    """Return active session or create a new inbox note + session."""
    session = await session_manager.get_session(chat_id)
    if session is not None:
        return session

    path = inbox_path()
    # Create empty note placeholder
    try:
        result = await gitea.create_file(path, "", f"note: start session {chat_id}")
        sha: str = result["content"]["sha"]
    except Exception:
        logger.exception("Failed to create inbox note for chat_id=%s", chat_id)
        sha = ""

    session = await session_manager.create_session(chat_id, path, sha)
    return session


# ---------------------------------------------------------------------------
# Debounce close callback
# ---------------------------------------------------------------------------


async def _debounce_close(chat_id: int, reason: str) -> None:
    """Called when debounce timer fires."""
    session = await session_manager.get_session(chat_id)
    if session is None:
        return
    bot: Bot = _ptb_app.bot  # type: ignore[union-attr]
    await assistant.close_session(session, bot, reason=reason)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        "Bot ready. Send any message to start a note."
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    session = await session_manager.get_session(chat_id)
    if session is None:
        await update.message.reply_text("No active session.")  # type: ignore[union-attr]
        return
    session_manager.cancel_debounce(chat_id)
    await assistant.close_session(session, _ptb_app.bot, reason="done")  # type: ignore[union-attr]


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    session = await session_manager.get_session(chat_id)
    if session is None:
        await update.message.reply_text("No active session.")  # type: ignore[union-attr]
        return
    session_manager.cancel_debounce(chat_id)
    # Delete the inbox note
    if session.note_path and session.note_sha:
        try:
            await gitea.delete_file(
                session.note_path, session.note_sha, f"note: cancel session {chat_id}"
            )
        except Exception:
            logger.exception("Failed to delete note on cancel for chat_id=%s", chat_id)
    await session_manager.mark_closed(chat_id, "cancelled")
    await update.message.reply_text("Session cancelled and note discarded.")  # type: ignore[union-attr]


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    notes = await session_manager.get_recent_notes(chat_id, limit=10)
    if not notes:
        await update.message.reply_text("No notes recorded yet.")  # type: ignore[union-attr]
        return
    lines = ["📋 *Recent notes:*\n"]
    for note in notes:
        lines.append(
            f"• `{note['slug']}` — {note['title']} ({note['destination']})"
        )
    await update.message.reply_text(  # type: ignore[union-attr]
        "\n".join(lines), parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    session = await session_manager.get_session(chat_id)
    if session is None:
        await update.message.reply_text("No active session.")  # type: ignore[union-attr]
        return

    msg_count = len(session.messages)
    started = session.started_at
    last = session.last_activity

    # Estimate time remaining on debounce
    try:
        last_dt = datetime.fromisoformat(last)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        remaining = max(0, settings.debounce_seconds - elapsed)
        remaining_str = f"{int(remaining // 60)}m {int(remaining % 60)}s"
    except Exception:
        remaining_str = "?"

    text = (
        f"📝 *Active session*\n"
        f"Note: `{session.note_path}`\n"
        f"Messages: {msg_count}\n"
        f"Started: {started}\n"
        f"Last activity: {last}\n"
        f"Auto-close in: {remaining_str}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")  # type: ignore[union-attr]


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return

    args = context.args or []
    slug = args[0] if args else None
    if not slug:
        await update.message.reply_text("Uso: /resume <slug>")  # type: ignore[union-attr]
        return

    # Close any active session first
    existing = await session_manager.get_session(chat_id)
    if existing is not None:
        session_manager.cancel_debounce(chat_id)
        await assistant.close_session(existing, _ptb_app.bot, reason="resume")  # type: ignore[union-attr]

    note_record = await session_manager.get_note_by_slug(chat_id, slug)
    if note_record is None:
        await update.message.reply_text(f"Slug not found: {slug}")  # type: ignore[union-attr]
        return

    note_path: str = note_record["final_path"]
    # Fetch current SHA
    file_data = await gitea.get_file(note_path)
    if file_data is None:
        await update.message.reply_text(  # type: ignore[union-attr]
            f"File not found in vault: {note_path}"
        )
        return

    sha: str = file_data["sha"]
    session = await session_manager.create_session(chat_id, note_path, sha)
    session_manager.schedule_debounce(chat_id)
    await update.message.reply_text(  # type: ignore[union-attr]
        f"Session resumed: `{note_path}`", parse_mode="Markdown"
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return
    text: str = update.effective_message.text or ""  # type: ignore[union-attr]

    session = await _get_or_create_session(chat_id)
    await assistant.process_message(session, text, _ptb_app.bot)  # type: ignore[union-attr]
    session_manager.schedule_debounce(chat_id)


async def handle_media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    if not _is_allowed(chat_id):
        return

    session = await _get_or_create_session(chat_id)

    try:
        obsidian_ref = await handle_media(
            update, session, session_manager, _ptb_app.bot  # type: ignore[union-attr]
        )
    except Exception:
        logger.exception("Failed to handle media for chat_id=%s", chat_id)
        await _ptb_app.bot.send_message(  # type: ignore[union-attr]
            chat_id=chat_id, text="❌ Failed to process media."
        )
        return

    if obsidian_ref:
        # Append reference to current note
        current_content = ""
        if session.note_path and session.note_sha:
            try:
                current_content = await gitea.read_text(session.note_path) or ""
            except Exception:
                pass
        new_content = (current_content.rstrip("\n") + "\n\n" + obsidian_ref + "\n").lstrip("\n")

        try:
            if session.note_sha:
                result = await gitea.update_file(
                    session.note_path,
                    new_content,
                    session.note_sha,
                    f"note: add media to {session.note_path}",
                )
                session.note_sha = result["content"]["sha"]
            else:
                result = await gitea.create_file(
                    session.note_path,
                    new_content,
                    f"note: add media to {session.note_path}",
                )
                session.note_sha = result["content"]["sha"]
            await session_manager.update_session(session)
        except Exception:
            logger.exception("Failed to update note with media ref for chat_id=%s", chat_id)

    session_manager.schedule_debounce(chat_id)


# ---------------------------------------------------------------------------
# Abandon-timeout watcher
# ---------------------------------------------------------------------------


async def _abandon_watcher() -> None:
    """Periodically close sessions inactive for longer than abandon_timeout_seconds."""
    while True:
        await asyncio.sleep(1800)  # check every 30 minutes
        try:
            chat_ids = await session_manager.get_abandoned_chat_ids()
            for chat_id in chat_ids:
                session = await session_manager.get_session(chat_id)
                if session is None:
                    continue
                logger.info("Closing abandoned session for chat_id=%s", chat_id)
                session_manager.cancel_debounce(chat_id)
                if _ptb_app is not None:
                    await assistant.close_session(session, _ptb_app.bot, reason="abandon")
        except Exception:
            logger.exception("Error in abandon watcher")


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ptb_app

    # Initialize database
    await session_manager.init_db()

    # Set debounce callback
    session_manager.set_close_callback(_debounce_close)

    # Build PTB application
    _ptb_app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .updater(None)  # webhook mode — no polling updater
        .build()
    )

    # Register handlers
    _ptb_app.add_handler(CommandHandler("start", cmd_start))
    _ptb_app.add_handler(CommandHandler("done", cmd_done))
    _ptb_app.add_handler(CommandHandler("cancel", cmd_cancel))
    _ptb_app.add_handler(CommandHandler("list", cmd_list))
    _ptb_app.add_handler(CommandHandler("status", cmd_status))
    _ptb_app.add_handler(CommandHandler("resume", cmd_resume))
    _ptb_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    _ptb_app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.VOICE | filters.AUDIO,
            handle_media_message,
        )
    )

    # Initialize and start PTB
    await _ptb_app.initialize()
    await _ptb_app.start()

    # Set webhook
    try:
        await _ptb_app.bot.set_webhook(
            url=settings.webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Webhook set to %s", settings.webhook_url)
    except Exception:
        logger.exception("Failed to set webhook")

    # Start abandon-timeout background task
    abandon_task = asyncio.create_task(_abandon_watcher(), name="abandon-watcher")

    yield

    # Shutdown
    abandon_task.cancel()
    try:
        await abandon_task
    except asyncio.CancelledError:
        pass
    try:
        await _ptb_app.bot.delete_webhook()
    except Exception:
        pass
    await _ptb_app.stop()
    await _ptb_app.shutdown()
    await gitea.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="telegram-obsidian-sync", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    data = await request.json()
    update = Update.de_json(data, _ptb_app.bot)  # type: ignore[union-attr]
    await _ptb_app.process_update(update)  # type: ignore[union-attr]
    return Response(status_code=200)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
