from __future__ import annotations

import base64
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class GiteaClient:
    """Async Gitea REST API client for vault operations."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{settings.gitea_base_url}/api/v1",
                headers={
                    "Authorization": f"token {settings.gitea_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    @property
    def _repo_prefix(self) -> str:
        return f"/repos/{settings.gitea_repo_owner}/{settings.gitea_repo_name}/contents"

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_file(self, path: str) -> dict | None:
        """GET file metadata (includes sha and base64 content). Returns None on 404."""
        client = self._get_client()
        response = await client.get(f"{self._repo_prefix}/{path}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def create_file(self, path: str, content: str, message: str) -> dict:
        """Create a new file. Content is plain text — will be base64-encoded internally."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        client = self._get_client()
        response = await client.post(
            f"{self._repo_prefix}/{path}",
            json={"message": message, "content": encoded},
        )
        response.raise_for_status()
        return response.json()

    async def update_file(self, path: str, content: str, sha: str, message: str) -> dict:
        """Update an existing file. Content is plain text — will be base64-encoded internally."""
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        client = self._get_client()
        response = await client.put(
            f"{self._repo_prefix}/{path}",
            json={"message": message, "content": encoded, "sha": sha},
        )
        response.raise_for_status()
        return response.json()

    async def delete_file(self, path: str, sha: str, message: str) -> None:
        """Delete a file by path and SHA."""
        client = self._get_client()
        response = await client.request(
            "DELETE",
            f"{self._repo_prefix}/{path}",
            json={"message": message, "sha": sha},
        )
        response.raise_for_status()

    async def move_file(self, src: str, dst: str, content: str, sha: str) -> str:
        """Move a file: create at dst then delete src. Returns new file SHA."""
        result = await self.create_file(dst, content, f"move: {src} → {dst}")
        new_sha: str = result["content"]["sha"]
        await self.delete_file(src, sha, f"move: remove {src}")
        return new_sha

    async def read_text(self, path: str) -> str | None:
        """GET file and base64-decode content. Returns None if file doesn't exist."""
        data = await self.get_file(path)
        if data is None:
            return None
        encoded: str = data["content"]
        # Gitea may include newlines in the base64 payload
        return base64.b64decode(encoded.replace("\n", "")).decode("utf-8")

    async def append_to_file(self, path: str, line: str) -> None:
        """Append a line to an existing file, creating it if it doesn't exist."""
        data = await self.get_file(path)
        if data is None:
            await self.create_file(path, line + "\n", f"append: {path}")
            return

        encoded: str = data["content"]
        current_content = base64.b64decode(encoded.replace("\n", "")).decode("utf-8")
        sha: str = data["sha"]
        new_content = current_content.rstrip("\n") + "\n" + line + "\n"
        await self.update_file(path, new_content, sha, f"append: {path}")

    async def create_file_bytes(self, path: str, content_bytes: bytes, message: str) -> dict:
        """Create a new file from raw bytes (e.g. media uploads)."""
        encoded = base64.b64encode(content_bytes).decode("ascii")
        client = self._get_client()
        response = await client.post(
            f"{self._repo_prefix}/{path}",
            json={"message": message, "content": encoded},
        )
        response.raise_for_status()
        return response.json()


gitea = GiteaClient()
