"""Artifacts plugin service.

Known v1 limitations
--------------------
The MCP tool handler (``ArtifactsToolProvider.handle_tool_call``, defined in
``virtual_tools.py``) does not currently receive ``conversation_id`` from the
MCP gateway / runner context, so artifacts created via the
``artifacts__create_artifact`` tool are persisted with ``conversation_id=NULL``.
This is intentional for v1: threading the conversation id through the gateway
kwargs is a v2 task (see TODO in ``handle_tool_call``), and until then
``/me/artifacts`` cannot filter tool-created artifacts by thread.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from plugins.artifacts import storage
from plugins.artifacts.hooks import (
    start_cleanup_worker,  # noqa: F401 — hook registration
)
from plugins.artifacts.models import Artifact
from plugins.artifacts.signing import build_capability_url

_logger = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS = 30
_DEFAULT_MAX_HTML = 2_097_152


def _load_config() -> dict:
    try:
        from core.plugin_registry.models import PluginRegistryEntry

        entry = PluginRegistryEntry.get_sync(name="artifacts")
        return (entry or {}).get("config") or {}
    except Exception:
        return {}


class ArtifactsService:
    """Facade for artifact creation, retrieval, and lifecycle actions."""

    async def create(
        self,
        *,
        title: str,
        html: str,
        agent_id: str,
        owner_user_id: str,
        conversation_id: str | None = None,
        pin: bool = False,
        ttl_days: int | None = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        cfg = _load_config()
        max_bytes = int(cfg.get("max_html_bytes", _DEFAULT_MAX_HTML))
        default_ttl = int(cfg.get("default_ttl_days", _DEFAULT_TTL_DAYS))
        ttl = ttl_days if ttl_days is not None else default_ttl

        storage.validate_html(html, max_bytes=max_bytes)
        content_hash, size_bytes = storage.compute_hash_and_size(html)

        artifact_id = str(uuid4())
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=ttl)

        file_path = storage.write_artifact(artifact_id, html)
        try:
            await Artifact.create(
                id=artifact_id,
                title=title,
                agent_id=agent_id,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                file_path=file_path,
                size_bytes=size_bytes,
                content_hash=content_hash,
                pinned=pin,
                expires_at=expires_at,
            )
        except Exception:
            storage.delete_artifact(artifact_id)
            raise

        return {
            "artifact_id": artifact_id,
            "url": build_capability_url(artifact_id),
            "expires_at": expires_at.isoformat(),
            "title": title,
        }

    async def pin(self, artifact_id: str, *, pinned: bool) -> None:
        if pinned:
            await Artifact.write(artifact_id, pinned=True)
        else:
            cfg = _load_config()
            ttl = int(cfg.get("default_ttl_days", _DEFAULT_TTL_DAYS))
            await Artifact.write(
                artifact_id,
                pinned=False,
                expires_at=datetime.now(UTC) + timedelta(days=ttl),
            )

    async def revoke(self, artifact_id: str) -> None:
        await Artifact.write(artifact_id, revoked_at=datetime.now(UTC))

    async def unrevoke(self, artifact_id: str) -> None:
        await Artifact.write(artifact_id, revoked_at=None)

    async def hard_delete(self, artifact_id: str) -> None:
        storage.delete_artifact(artifact_id)
        await Artifact.delete(artifact_id)
