# CLAUDE.md

## Stack
- Python 3.12, FastAPI, python-telegram-bot v21, Anthropic SDK, httpx, aiosqlite
- Session state in SQLite at /data/db.sqlite
- Vault stored in Gitea — all operations via REST API, no local clone

## Key modules
- `app/config.py` — all env vars via pydantic-settings; singleton `settings`
- `app/session.py` — `SessionManager` singleton; debounce via asyncio tasks
- `app/assistant.py` — `Assistant` singleton; uses Claude tool-use for structured output
- `app/vault/gitea.py` — `GiteaClient` singleton; always needs file SHA for updates/deletes
- `app/vault/writer.py` — pure functions; no I/O

## Patterns
- All Gitea operations are async with httpx
- Claude calls use prompt caching on system prompt
- Whitelist check is the first thing every Telegram handler does — unauthorized messages are silently ignored
- SHA must be current; always re-fetch if unsure

## Secrets
Never generate tokens or secrets — always ask the user.
