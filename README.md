# telegram-obsidian-sync

A FastAPI service that acts as a Telegram webhook receiver and syncs messages to an [Obsidian](https://obsidian.md/) vault stored in a self-hosted [Gitea](https://gitea.io/) repository. Incoming messages are processed by Claude Sonnet via the Anthropic SDK and persisted as structured Markdown notes.

## How it works

1. A Telegram message hits `POST /webhook` via Traefik.
2. `session.py` looks up (or creates) an active session for the sender's `chat_id`.
3. `assistant.py` calls Claude Sonnet with the full conversation history and the current note content. Claude uses forced tool-use to return either a note rewrite or a chat reply (or both).
4. The updated note is committed to Gitea via `vault/gitea.py` (REST API, base64-encoded content).
5. A 3-minute debounce timer runs per session. When it fires (or on `/done`), Claude is called again to classify the content, pick a destination, and produce a final polished note. The inbox draft is moved (create + delete) to the destination path.
6. The Obsidian client on the user's device pulls changes via the Obsidian Git plugin.

### Session lifecycle

```
first message → session open → note created at Inbox/YYYY-MM-DD HH:MM.md
                 ↓ each message
           Claude rewrites note (silent) or replies to chat
                 ↓ 3 min inactivity | /done
           Claude classifies → note moved to destination
                 ↓
           ✅ Saved to <destination> · /resume <slug>
```

A 6-hour abandon watcher runs as a background task and closes sessions that have been silent longer than `ABANDON_TIMEOUT_SECONDS`.

### Destinations

| Destination | Vault path                                   | Note format                               |
| ----------- | -------------------------------------------- | ----------------------------------------- |
| `inbox`     | `Inbox/YYYY-MM-DD HH:MM.md`                  | Free-form Markdown                        |
| `recurso`   | `Recursos/links/YYYY-MM-DD <title>.md`       | Frontmatter with `url`, `tags`, `summary` |
| `ideia`     | `Notas/Ideias/YYYY-MM-DD <title>.md`         | Free-form with inferred tags              |
| `tarefa`    | append to `Board.md` + optional context note | `obsidian-tasks` task line                |

## Project structure

```
app/
├── main.py          # FastAPI app, lifespan, Telegram handlers
├── config.py        # pydantic-settings; parses GITEA_VAULT_REPO into base_url/owner/repo
├── assistant.py     # Claude tool-use loop: process_message + close_session
├── session.py       # SQLite-backed sessions, asyncio debounce tasks
├── handlers/
│   └── media.py     # Telegram media download → Gitea asset upload
└── vault/
    ├── gitea.py     # Async httpx Gitea REST API client
    └── writer.py    # Obsidian Markdown + frontmatter builders (pure functions)
```

## Configuration

Copy `.env.example` to `.env` and fill in all values.

| Variable             | Description                                                                           |
| -------------------- | ------------------------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather                                                                  |
| `WEBHOOK_URL`        | Public HTTPS URL for the webhook endpoint (e.g. `https://sync.example.com/webhook`)   |
| `ALLOWED_CHAT_IDS`   | Comma-separated list of authorised Telegram `chat_id`s                                |
| `GITEA_VAULT_REPO`   | Full URL of the Gitea vault repository, e.g. `https://git.example.com/user/vault.git` |
| `GITEA_TOKEN`        | Gitea personal access token with read/write access to the vault repo                  |
| `ANTHROPIC_API_KEY`  | Anthropic API key                                                                     |
| `NOTE_TIMEZONE`      | IANA timezone for note timestamps (default: `America/Sao_Paulo`)                      |

Optional tunables (set in environment, not required in `.env`):

| Variable                  | Default           | Description                                   |
| ------------------------- | ----------------- | --------------------------------------------- |
| `DEBOUNCE_SECONDS`        | `180`             | Inactivity timeout before session auto-closes |
| `ABANDON_TIMEOUT_SECONDS` | `21600`           | Absolute session timeout (6 h)                |
| `DB_PATH`                 | `/data/db.sqlite` | SQLite database path                          |

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in .env

uvicorn app.main:app --reload --port 8000
```

The service registers the Telegram webhook on startup. For local development, expose port 8000 via a tunnel (e.g. `ngrok http 8000`) and set `WEBHOOK_URL` to the tunnel URL.

## Running with Docker

```bash
docker build -t telegram-obsidian-sync .
docker run --env-file .env -v /opt/data/telegram-obsidian-sync:/data -p 8000:8000 telegram-obsidian-sync
```

## Deployment

The service is deployed via Docker Compose alongside a Traefik reverse proxy (see `codelab-infra/compose/telegram-obsidian-sync/`). Traefik handles TLS termination with a Cloudflare DNS-01 wildcard certificate.

The SQLite database and any local data are persisted at `/opt/data/telegram-obsidian-sync/` on the host.

## Vault prerequisites

- `Board.md` must exist at the vault root for `tarefa` destination to append task lines.
- `Projetos/Telegram-Obsidian Sync/bot-instructions.md` is read at the start of each session and injected into the Claude system prompt. Edit this file to customise assistant behaviour (classification rules, preferred destinations, note format) without redeploying.

## Gitea API operations

All vault mutations are single HTTP calls — there is no local clone.

| Operation   | Endpoint                                                                     |
| ----------- | ---------------------------------------------------------------------------- |
| Read file   | `GET /api/v1/repos/{owner}/{repo}/contents/{path}`                           |
| Create file | `POST /api/v1/repos/{owner}/{repo}/contents/{path}`                          |
| Update file | `PUT /api/v1/repos/{owner}/{repo}/contents/{path}` (requires current SHA)    |
| Delete file | `DELETE /api/v1/repos/{owner}/{repo}/contents/{path}` (requires current SHA) |
| Move file   | Create at destination + delete at source                                     |

The SHA returned by each write operation is stored in the session and passed to subsequent updates. If the SHA is ever stale, Gitea returns 409 — re-fetch the file to get the current SHA.

## Claude integration

`assistant.py` uses forced tool-use (`tool_choice: {"type": "tool", "name": "..."}`) so Claude always returns structured JSON rather than free text.

- **`process_message` tool** — called on each incoming message; returns `action` (`update_note`, `reply`, `update_note_and_reply`, or `clear_note`) plus optional `note_content` and `reply_text`.
- **`close_session` tool** — called when closing; returns `destination`, `title`, `slug`, `note_content`, `tags`, and destination-specific fields (`url`, `task_line`).

The system prompt is sent with `cache_control: ephemeral` to enable prompt caching and reduce cost on repeated calls within a session.

## Telegram commands

| Command          | Behaviour                                                            |
| ---------------- | -------------------------------------------------------------------- |
| `/done`          | Force-close the active session immediately                           |
| `/cancel`        | Discard the active session and delete the draft note                 |
| `/resume <slug>` | Re-open a closed note; closes any active session first               |
| `/list`          | Show the 10 most recently closed notes (slug + destination)          |
| `/status`        | Show active session path, message count, and debounce time remaining |
