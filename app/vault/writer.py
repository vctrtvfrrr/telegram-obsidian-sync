from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pytz

from app.config import settings


def _now_local() -> datetime:
    tz = pytz.timezone(settings.note_timezone)
    return datetime.now(tz)


def _fmt_date(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = _now_local()
    return dt.strftime("%Y-%m-%d")


def _fmt_datetime(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = _now_local()
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def inbox_path(dt: Optional[datetime] = None) -> str:
    """Returns vault path for an inbox note: Inbox/YYYY-MM-DD HH:MM.md"""
    if dt is None:
        dt = _now_local()
    return f"Inbox/{dt.strftime('%Y-%m-%d %H:%M')}.md"


def note_path_for_destination(destination: str, title: str, dt: Optional[datetime] = None) -> str:
    """Return vault path for a note based on its destination."""
    if dt is None:
        dt = _now_local()
    date_str = _fmt_date(dt)

    if destination == "inbox":
        return inbox_path(dt)
    elif destination == "recurso":
        safe_title = _safe_filename(title)
        return f"Recursos/links/{date_str} {safe_title}.md"
    elif destination == "ideia":
        safe_title = _safe_filename(title)
        return f"Notas/Ideias/{date_str} {safe_title}.md"
    elif destination == "tarefa":
        safe_title = _safe_filename(title)
        return f"Notas/Tarefas/{date_str} {safe_title}.md"
    else:
        safe_title = _safe_filename(title)
        return f"Notas/{date_str} {safe_title}.md"


def asset_path(destination: str, filename: str) -> str:
    """Return vault path for an asset file under the destination directory."""
    if destination == "inbox":
        return f"Inbox/assets/{filename}"
    elif destination == "recurso":
        return f"Recursos/links/assets/{filename}"
    elif destination == "ideia":
        return f"Notas/Ideias/assets/{filename}"
    elif destination == "tarefa":
        return f"Notas/Tarefas/assets/{filename}"
    else:
        return f"Notas/assets/{filename}"


def _safe_filename(title: str) -> str:
    """Convert a title to a safe filename by replacing forbidden chars."""
    for ch in r'\/:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip()


# ---------------------------------------------------------------------------
# Note content builders
# ---------------------------------------------------------------------------


def build_note_for_destination(
    destination: str,
    title: str,
    content: str,
    **kwargs: Any,
) -> str:
    """Dispatch to the right format builder and return complete Obsidian Markdown."""
    if destination == "inbox":
        return _build_inbox_note(title, content, **kwargs)
    elif destination == "recurso":
        return _build_recurso_note(title, content, **kwargs)
    elif destination == "ideia":
        return _build_ideia_note(title, content, **kwargs)
    elif destination == "tarefa":
        return _build_tarefa_note(title, content, **kwargs)
    else:
        return _build_generic_note(title, content, **kwargs)


def _build_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {item}")
            else:
                lines.append(f"{key}: []")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _build_inbox_note(title: str, content: str, **kwargs: Any) -> str:
    dt = kwargs.get("dt") or _now_local()
    tags: list[str] = kwargs.get("tags", [])
    frontmatter = _build_frontmatter(
        {
            "title": title,
            "date": _fmt_datetime(dt),
            "tags": tags,
        }
    )
    return f"{frontmatter}\n\n# {title}\n\n{content}\n"


def _build_recurso_note(title: str, content: str, **kwargs: Any) -> str:
    dt = kwargs.get("dt") or _now_local()
    tags: list[str] = kwargs.get("tags", [])
    url: str = kwargs.get("url", "")
    summary: str = kwargs.get("summary", "")
    frontmatter = _build_frontmatter(
        {
            "title": title,
            "date": _fmt_date(dt),
            "url": url,
            "tags": tags,
            "summary": summary,
        }
    )
    body = content
    if url and f"[{url}]" not in body and url not in body:
        body = f"**URL:** {url}\n\n{body}"
    return f"{frontmatter}\n\n# {title}\n\n{body}\n"


def _build_ideia_note(title: str, content: str, **kwargs: Any) -> str:
    dt = kwargs.get("dt") or _now_local()
    tags: list[str] = kwargs.get("tags", [])
    frontmatter = _build_frontmatter(
        {
            "title": title,
            "date": _fmt_date(dt),
            "tags": tags,
        }
    )
    return f"{frontmatter}\n\n# {title}\n\n{content}\n"


def _build_tarefa_note(title: str, content: str, **kwargs: Any) -> str:
    dt = kwargs.get("dt") or _now_local()
    tags: list[str] = kwargs.get("tags", [])
    task_line: str = kwargs.get("task_line", "")
    frontmatter = _build_frontmatter(
        {
            "title": title,
            "date": _fmt_date(dt),
            "tags": tags,
        }
    )
    body = content
    if task_line:
        body = f"{task_line}\n\n{body}"
    return f"{frontmatter}\n\n# {title}\n\n{body}\n"


def _build_generic_note(title: str, content: str, **kwargs: Any) -> str:
    dt = kwargs.get("dt") or _now_local()
    tags: list[str] = kwargs.get("tags", [])
    frontmatter = _build_frontmatter(
        {
            "title": title,
            "date": _fmt_datetime(dt),
            "tags": tags,
        }
    )
    return f"{frontmatter}\n\n# {title}\n\n{content}\n"
