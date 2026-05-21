from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Session:
    chat_id: int
    note_path: str
    note_sha: str
    started_at: str  # ISO 8601 UTC
    last_activity: str  # ISO 8601 UTC
    messages: list[dict] = field(default_factory=list)
    status: str = "active"


CloseCallbackType = Callable[[int, str], Coroutine[Any, Any, None]]


class SessionManager:
    def __init__(self) -> None:
        self._db_path = settings.db_path
        self._debounce_tasks: dict[int, asyncio.Task] = {}
        self._close_callback: Optional[CloseCallbackType] = None

    def set_close_callback(self, fn: CloseCallbackType) -> None:
        self._close_callback = fn

    async def init_db(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id      INTEGER PRIMARY KEY,
                    note_path    TEXT NOT NULL,
                    note_sha     TEXT NOT NULL,
                    started_at   TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    messages     TEXT NOT NULL DEFAULT '[]',
                    status       TEXT NOT NULL DEFAULT 'active'
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS note_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL,
                    slug        TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    final_path  TEXT NOT NULL,
                    title       TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def get_session(self, chat_id: int) -> Session | None:
        """Return the active session for chat_id, or None if not found."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sessions WHERE chat_id = ? AND status = 'active'",
                (chat_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return Session(
                    chat_id=row["chat_id"],
                    note_path=row["note_path"],
                    note_sha=row["note_sha"],
                    started_at=row["started_at"],
                    last_activity=row["last_activity"],
                    messages=json.loads(row["messages"]),
                    status=row["status"],
                )

    async def create_session(
        self, chat_id: int, note_path: str, note_sha: str
    ) -> Session:
        """Create and persist a new active session."""
        now = _utcnow()
        session = Session(
            chat_id=chat_id,
            note_path=note_path,
            note_sha=note_sha,
            started_at=now,
            last_activity=now,
            messages=[],
            status="active",
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sessions
                    (chat_id, note_path, note_sha, started_at, last_activity, messages, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.chat_id,
                    session.note_path,
                    session.note_sha,
                    session.started_at,
                    session.last_activity,
                    json.dumps(session.messages),
                    session.status,
                ),
            )
            await db.commit()
        return session

    async def update_session(self, session: Session) -> None:
        """Persist messages, note_path, note_sha, and last_activity."""
        session.last_activity = _utcnow()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE sessions
                SET note_path = ?, note_sha = ?, last_activity = ?, messages = ?
                WHERE chat_id = ?
                """,
                (
                    session.note_path,
                    session.note_sha,
                    session.last_activity,
                    json.dumps(session.messages),
                    session.chat_id,
                ),
            )
            await db.commit()

    async def mark_closed(self, chat_id: int, status: str = "closed") -> None:
        """Mark a session as closed (or cancelled, debounce, etc.)."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE sessions SET status = ? WHERE chat_id = ? AND status = 'active'",
                (status, chat_id),
            )
            await db.commit()

    async def save_note_history(
        self,
        chat_id: int,
        slug: str,
        destination: str,
        final_path: str,
        title: str,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO note_history (chat_id, slug, destination, final_path, title, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, slug, destination, final_path, title, _utcnow()),
            )
            await db.commit()

    async def get_recent_notes(self, chat_id: int, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT slug, destination, final_path, title, created_at
                FROM note_history
                WHERE chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_note_by_slug(self, chat_id: int, slug: str) -> dict | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT slug, destination, final_path, title, created_at
                FROM note_history
                WHERE chat_id = ? AND slug = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (chat_id, slug),
            ) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                return dict(row)

    def schedule_debounce(self, chat_id: int) -> None:
        """Cancel existing debounce task (if any) and start a new one."""
        self.cancel_debounce(chat_id)

        async def _debounce_task() -> None:
            await asyncio.sleep(settings.debounce_seconds)
            if self._close_callback is not None:
                try:
                    await self._close_callback(chat_id, "debounce")
                except Exception:
                    logger.exception("Error in debounce close_callback for chat_id=%s", chat_id)

        task = asyncio.create_task(_debounce_task(), name=f"debounce-{chat_id}")
        self._debounce_tasks[chat_id] = task

    def cancel_debounce(self, chat_id: int) -> None:
        task = self._debounce_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def get_all_active_chat_ids(self) -> list[int]:
        """Return chat_ids of all currently active sessions."""
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id FROM sessions WHERE status = 'active'"
            ) as cursor:
                rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]

    async def get_abandoned_chat_ids(self) -> list[int]:
        """Return chat_ids of active sessions inactive longer than abandon_timeout_seconds."""
        cutoff = datetime.now(timezone.utc).timestamp() - settings.abandon_timeout_seconds
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT chat_id, last_activity FROM sessions WHERE status = 'active'"
            ) as cursor:
                rows = await cursor.fetchall()

        abandoned = []
        for row in rows:
            try:
                last = datetime.fromisoformat(row["last_activity"])
                if last.timestamp() < cutoff:
                    abandoned.append(row["chat_id"])
            except Exception:
                pass
        return abandoned


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


session_manager = SessionManager()
