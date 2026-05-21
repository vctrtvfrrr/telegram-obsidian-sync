from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Optional

import anthropic
import httpx

from app.config import settings
from app.session import Session, session_manager
from app.vault.gitea import gitea
from app.vault.writer import build_note_for_destination, note_path_for_destination

logger = logging.getLogger(__name__)

# Module-level cache: chat_id -> bot_instructions string
_instructions_cache: dict[int, str] = {}

SYSTEM_PROMPT = """\
You are an intelligent secretary managing an Obsidian vault via Telegram.

Process each user message and decide the appropriate action:
- Note content / resource / idea / task → rewrite the entire note in Obsidian Markdown (action: update_note)
- Question or request for opinion → answer the user directly; preserve relevant insight in note if valuable (action: update_note_and_reply or reply)
- Explicit classification instruction → acknowledge briefly (action: reply), note any instruction for later
- "Start over" / "reset" → clear note content, keep session (action: clear_note)

Always use the process_message tool. Never respond with plain text.
For note content, always write complete Obsidian Markdown including a level-1 heading title.
The "Current note content" block in the system context is background reference only — never copy it verbatim into note_content.\
"""

CLOSE_SYSTEM_PROMPT = """\
You are an intelligent secretary finalizing an Obsidian vault note captured via Telegram.

Analyze the entire conversation and produce structured output for the close_session tool:
- Choose the best destination: inbox, recurso (web resource/link), tarefa (task/todo), or ideia (idea/thought)
- Write the complete, polished final note in Obsidian Markdown
- Generate a short slug (MMDD-short-title format, e.g. "0517-artigo-sobre-ia")
- Extract relevant tags
- For recurso: include the URL
- For tarefa: provide an obsidian-tasks compatible task line, and indicate if there is secondary content besides the task

Always use the close_session tool. Never respond with plain text.\
"""

PROCESS_MESSAGE_TOOL: dict[str, Any] = {
    "name": "process_message",
    "description": "Process a user message and take the appropriate action on the note.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["update_note", "reply", "update_note_and_reply", "clear_note"],
                "description": "Action to take: update_note rewrites the note, reply sends a message to the user, update_note_and_reply does both, clear_note resets the note.",
            },
            "note_content": {
                "type": "string",
                "description": "Full Obsidian Markdown content for the note (required when action is update_note or update_note_and_reply).",
            },
            "reply_text": {
                "type": "string",
                "description": "Text to send to the user (required when action is reply or update_note_and_reply).",
            },
        },
        "required": ["action"],
    },
}

CLOSE_SESSION_TOOL: dict[str, Any] = {
    "name": "close_session",
    "description": "Finalize and classify the note, then save it to the appropriate vault location.",
    "input_schema": {
        "type": "object",
        "properties": {
            "destination": {
                "type": "string",
                "enum": ["inbox", "recurso", "tarefa", "ideia"],
                "description": "Where to save the note.",
            },
            "title": {
                "type": "string",
                "description": "Note title.",
            },
            "slug": {
                "type": "string",
                "description": "Short slug in MMDD-short-title format, e.g. '0517-artigo-sobre-ia'.",
            },
            "note_content": {
                "type": "string",
                "description": "Full final note content in Obsidian Markdown.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of tags for the note.",
            },
            "url": {
                "type": "string",
                "description": "URL for recurso destination.",
            },
            "task_line": {
                "type": "string",
                "description": "obsidian-tasks format task line, for tarefa destination.",
            },
            "has_secondary_content": {
                "type": "boolean",
                "description": "For tarefa: whether there is content besides the task line.",
            },
        },
        "required": ["destination", "title", "slug", "note_content", "tags"],
    },
}


class Assistant:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def _load_instructions(self, chat_id: int) -> str:
        """Load bot instructions from Gitea (cached per session)."""
        if chat_id in _instructions_cache:
            return _instructions_cache[chat_id]
        try:
            text = await gitea.read_text("Projetos/Telegram-Obsidian Sync/bot-instructions.md")
            instructions = text or ""
        except Exception:
            logger.debug("No bot-instructions.md found in vault")
            instructions = ""
        _instructions_cache[chat_id] = instructions
        return instructions

    def _build_system_blocks(self, base_prompt: str, extra: str = "") -> list[dict]:
        """Build system blocks with prompt caching."""
        blocks: list[dict] = [
            {
                "type": "text",
                "text": base_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if extra:
            blocks.append(
                {
                    "type": "text",
                    "text": extra,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    async def process_message(self, session: Session, text: str, bot: Any) -> None:
        """Process a single user message, call Claude, and apply the result."""
        instructions = await self._load_instructions(session.chat_id)

        # Append user message to history
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        session.messages.append({"role": "user", "content": text, "timestamp": now_iso})

        # Read current note and refresh SHA to avoid stale-SHA conflicts
        current_note: str = ""
        if session.note_path:
            try:
                file_data = await gitea.get_file(session.note_path)
                if file_data:
                    current_note = base64.b64decode(
                        file_data["content"].replace("\n", "")
                    ).decode("utf-8")
                    session.note_sha = file_data["sha"]
            except Exception:
                logger.warning("Could not read current note: %s", session.note_path)

        # Build context block
        context_parts = []
        if current_note:
            context_parts.append(f"## Current note content\n\n```markdown\n{current_note}\n```")
        if context_parts:
            context_block = "\n\n".join(context_parts)
        else:
            context_block = "The note is empty — this is the first message."

        system_blocks = self._build_system_blocks(SYSTEM_PROMPT, instructions)
        system_blocks.append({"type": "text", "text": context_block})

        # Build messages list (only role/content for Anthropic API)
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in session.messages
        ]

        response = await self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_blocks,
            tools=[PROCESS_MESSAGE_TOOL],
            tool_choice={"type": "tool", "name": "process_message"},
            messages=api_messages,
        )

        tool_input = self._extract_tool_input(response)
        action: str = tool_input.get("action", "reply")
        note_content: str = tool_input.get("note_content", "")
        reply_text: str = tool_input.get("reply_text", "")

        assistant_summary_parts: list[str] = []

        if action in ("update_note", "update_note_and_reply"):
            if note_content:
                try:
                    updated_data = await self._upsert_note(session, note_content)
                    session.note_sha = updated_data
                    assistant_summary_parts.append("[Note updated]")
                except Exception:
                    logger.warning("Failed to upsert note for chat_id=%s", session.chat_id)
                    await bot.send_message(
                        chat_id=session.chat_id,
                        text="⚠️ Could not save note. Please try again.",
                    )

        if action == "clear_note":
            cleared_content = "*(note cleared)*\n"
            try:
                sha = await self._upsert_note(session, cleared_content)
                session.note_sha = sha
                assistant_summary_parts.append("[Note cleared]")
                reply_text = reply_text or "Note cleared. Send new content to start fresh."
            except Exception:
                logger.warning("Failed to clear note for chat_id=%s", session.chat_id)
                await bot.send_message(
                    chat_id=session.chat_id,
                    text="⚠️ Could not clear note. Please try again.",
                )

        if action in ("reply", "update_note_and_reply", "clear_note") and reply_text:
            await bot.send_message(chat_id=session.chat_id, text=reply_text)
            assistant_summary_parts.append(f"[Replied: {reply_text[:80]}]")

        assistant_content = " | ".join(assistant_summary_parts) if assistant_summary_parts else "[processed]"
        session.messages.append(
            {"role": "assistant", "content": assistant_content, "timestamp": now_iso}
        )
        await session_manager.update_session(session)

    async def _upsert_note(self, session: Session, content: str) -> str:
        """Create or update the session note. Returns new SHA."""
        if not session.note_sha:
            result = await gitea.create_file(
                session.note_path, content, f"note: create {session.note_path}"
            )
            return result["content"]["sha"]

        try:
            result = await gitea.update_file(
                session.note_path, content, session.note_sha, f"note: update {session.note_path}"
            )
            return result["content"]["sha"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            # SHA stale — re-fetch and retry once
            logger.warning(
                "SHA conflict for %s (chat_id=%s), re-fetching and retrying",
                session.note_path,
                session.chat_id,
            )
            file_data = await gitea.get_file(session.note_path)
            if file_data is None:
                raise
            session.note_sha = file_data["sha"]
            result = await gitea.update_file(
                session.note_path, content, session.note_sha, f"note: update {session.note_path}"
            )
            return result["content"]["sha"]

    def _extract_tool_input(self, response: Any) -> dict:
        """Extract tool_use input from Claude response."""
        for block in response.content:
            if block.type == "tool_use":
                return block.input  # type: ignore[return-value]
        return {}

    async def close_session(self, session: Session, bot: Any, reason: str = "debounce") -> None:
        """Finalize the note: classify it via Claude, move to destination, save history."""
        await bot.send_message(chat_id=session.chat_id, text="⏳ Processing note...")

        # Read current note
        current_note: str = ""
        if session.note_path:
            try:
                current_note = await gitea.read_text(session.note_path) or ""
            except Exception:
                logger.warning("Could not read note for closing: %s", session.note_path)

        # Build messages for Claude
        api_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in session.messages
        ]
        if not api_messages:
            api_messages = [{"role": "user", "content": "(empty session)"}]

        # Add current note as final context message
        if current_note:
            api_messages.append(
                {
                    "role": "user",
                    "content": f"## Current note content (for finalization)\n\n```markdown\n{current_note}\n```\n\nPlease finalize this note now.",
                }
            )

        system_blocks = self._build_system_blocks(CLOSE_SYSTEM_PROMPT)

        # Retry loop
        max_retries = 3
        backoff = 30
        tool_input: dict = {}

        for attempt in range(1, max_retries + 1):
            try:
                response = await self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=system_blocks,
                    tools=[CLOSE_SESSION_TOOL],
                    tool_choice={"type": "tool", "name": "close_session"},
                    messages=api_messages,
                )
                tool_input = self._extract_tool_input(response)
                if tool_input:
                    break
                logger.warning("close_session: empty tool input (attempt %d)", attempt)
            except Exception as exc:
                logger.exception("close_session: attempt %d failed: %s", attempt, exc)
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                else:
                    # All retries exhausted
                    await bot.send_message(
                        chat_id=session.chat_id,
                        text=f"❌ Failed to process note. Draft saved at: {session.note_path}",
                    )
                    await session_manager.mark_closed(session.chat_id, "error")
                    _instructions_cache.pop(session.chat_id, None)
                    session_manager.cancel_debounce(session.chat_id)
                    return

        if not tool_input:
            await bot.send_message(
                chat_id=session.chat_id,
                text=f"❌ Failed to process note. Draft saved at: {session.note_path}",
            )
            await session_manager.mark_closed(session.chat_id, "error")
            _instructions_cache.pop(session.chat_id, None)
            session_manager.cancel_debounce(session.chat_id)
            return

        destination: str = tool_input.get("destination", "inbox")
        title: str = tool_input.get("title", "Note")
        slug: str = tool_input.get("slug", "")
        note_content: str = tool_input.get("note_content", current_note)
        tags: list[str] = tool_input.get("tags", [])
        url: str = tool_input.get("url", "")
        task_line: str = tool_input.get("task_line", "")
        has_secondary: bool = tool_input.get("has_secondary_content", False)

        # For inbox, keep the existing path; for others compute the destination path
        final_path = session.note_path if destination == "inbox" else note_path_for_destination(destination, title)

        try:
            if destination == "tarefa":
                # Append task line to Board.md
                if task_line:
                    await gitea.append_to_file("Board.md", task_line)

                if has_secondary:
                    # Build and save secondary note content
                    secondary_content = build_note_for_destination(
                        destination, title, note_content, tags=tags, task_line=task_line
                    )
                    if session.note_sha:
                        await gitea.move_file(
                            session.note_path, final_path, secondary_content, session.note_sha
                        )
                    else:
                        await gitea.create_file(final_path, secondary_content, f"note: {title}")
                else:
                    # Delete inbox note (no secondary content)
                    if session.note_sha:
                        await gitea.delete_file(
                            session.note_path, session.note_sha, f"note: close task {title}"
                        )

            elif destination == "inbox":
                # Update in place
                built_content = build_note_for_destination(
                    destination, title, note_content, tags=tags, url=url
                )
                if session.note_sha:
                    await gitea.update_file(
                        session.note_path, built_content, session.note_sha, f"note: finalize {title}"
                    )
                else:
                    await gitea.create_file(session.note_path, built_content, f"note: {title}")
            else:
                # Move to destination
                built_content = build_note_for_destination(
                    destination, title, note_content, tags=tags, url=url
                )
                if session.note_sha:
                    await gitea.move_file(
                        session.note_path, final_path, built_content, session.note_sha
                    )
                else:
                    await gitea.create_file(final_path, built_content, f"note: {title}")

        except Exception as exc:
            logger.exception("close_session: failed to save note: %s", exc)
            await bot.send_message(
                chat_id=session.chat_id,
                text=f"❌ Failed to save note. Draft at: {session.note_path}",
            )
            await session_manager.mark_closed(session.chat_id, "error")
            _instructions_cache.pop(session.chat_id, None)
            session_manager.cancel_debounce(session.chat_id)
            return

        # Save history
        if slug:
            await session_manager.save_note_history(
                session.chat_id, slug, destination, final_path, title
            )

        # Mark session closed
        await session_manager.mark_closed(session.chat_id, "closed")
        _instructions_cache.pop(session.chat_id, None)
        session_manager.cancel_debounce(session.chat_id)

        # Notify user
        resume_hint = f" · /resume {slug}" if slug else ""
        await bot.send_message(
            chat_id=session.chat_id,
            text=f"✅ Saved to {destination}{resume_hint}",
        )


assistant = Assistant()
