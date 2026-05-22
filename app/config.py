from __future__ import annotations

import re
from functools import cached_property
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    webhook_url: str = "https://sync.victor.etc.br/webhook"
    allowed_chat_ids: str = ""
    gitea_vault_repo: str
    gitea_token: str
    anthropic_api_key: str
    openai_api_key: str | None = None
    note_timezone: str = "America/Sao_Paulo"

    db_path: str = "/data/db.sqlite"
    debounce_seconds: int = 180
    abandon_timeout_seconds: int = 21600

    @cached_property
    def allowed_chat_ids_list(self) -> list[int]:
        if not self.allowed_chat_ids.strip():
            return []
        return [int(cid.strip()) for cid in self.allowed_chat_ids.split(",") if cid.strip()]

    @cached_property
    def gitea_base_url(self) -> str:
        """Extract base URL from GITEA_VAULT_REPO (scheme + host)."""
        match = re.match(r"(https?://[^/]+)", self.gitea_vault_repo)
        if not match:
            raise ValueError(f"Cannot parse GITEA_VAULT_REPO: {self.gitea_vault_repo!r}")
        return match.group(1)

    @cached_property
    def gitea_repo_owner(self) -> str:
        """Extract owner from GITEA_VAULT_REPO path."""
        match = re.match(r"https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?$", self.gitea_vault_repo)
        if not match:
            raise ValueError(f"Cannot parse owner from GITEA_VAULT_REPO: {self.gitea_vault_repo!r}")
        return match.group(1)

    @cached_property
    def gitea_repo_name(self) -> str:
        """Extract repo name from GITEA_VAULT_REPO path."""
        match = re.match(r"https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?$", self.gitea_vault_repo)
        if not match:
            raise ValueError(f"Cannot parse repo name from GITEA_VAULT_REPO: {self.gitea_vault_repo!r}")
        return match.group(2)


settings = Settings()
