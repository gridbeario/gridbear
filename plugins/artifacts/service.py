"""Artifacts plugin service + MCP LocalToolProvider.

Known v1 limitations
--------------------
The MCP tool handler (``ArtifactsToolProvider.handle_tool_call``) does not
currently receive ``conversation_id`` from the MCP gateway / runner context,
so artifacts created via the ``artifacts__create_artifact`` tool are persisted
with ``conversation_id=NULL``. This is intentional for v1: threading the
conversation id through the gateway kwargs is a v2 task (see TODO in
``handle_tool_call``), and until then ``/me/artifacts`` cannot filter
tool-created artifacts by thread.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from core.interfaces.local_tools import LocalToolProvider
from plugins.artifacts import storage
from plugins.artifacts.hooks import (
    start_cleanup_worker,  # noqa: F401 — hook registration
)
from plugins.artifacts.models import Artifact
from plugins.artifacts.signing import build_capability_url

_logger = logging.getLogger(__name__)

_SERVER_NAME = "artifacts"
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


_TOOLS = [
    {
        "name": "artifacts__create_artifact",
        "description": (
            "Create a standalone HTML artifact (dashboard, chart, data viewer). "
            "Returns a public URL the user can click. Keep HTML self-contained: "
            "inline CSS and JS. External libraries only from esm.sh / unpkg / "
            "cdn.jsdelivr.net. Embed data directly; the CSP blocks runtime fetch. "
            "Pass pin=true to exempt the 30-day TTL."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "html"],
            "properties": {
                "title": {"type": "string", "maxLength": 200},
                "html": {"type": "string"},
                "pin": {"type": "boolean"},
                "ttl_days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
        },
    }
]


class ArtifactsToolProvider(LocalToolProvider):
    """Expose the create_artifact MCP tool to agents."""

    def __init__(self) -> None:
        self._service = ArtifactsService()

    def get_server_name(self) -> str:
        return _SERVER_NAME

    def get_tools(self) -> list[dict]:
        return list(_TOOLS)

    async def handle_tool_call(
        self, tool_name: str, arguments: dict, **kwargs
    ) -> list[dict]:
        if tool_name != "artifacts__create_artifact":
            return [{"type": "text", "text": f"Unknown artifacts tool: {tool_name}"}]

        # TODO(v2): thread conversation_id through from the MCP gateway
        # (kwargs.get("conversation_id")) so /me/artifacts can filter by thread.
        # Current v1 leaves the field NULL for tool-created artifacts.
        agent_name = kwargs.get("agent_name")
        oauth2_user = kwargs.get("oauth2_user")
        if not agent_name or not oauth2_user:
            _logger.error(
                "artifacts__create_artifact invoked without agent/user context "
                "(agent=%r, user=%r) — refusing",
                agent_name,
                oauth2_user,
            )
            return [
                {
                    "type": "text",
                    "text": (
                        "Error: artifact creation requires authenticated "
                        "agent and user context."
                    ),
                }
            ]

        try:
            result = await self._service.create(
                title=arguments["title"],
                html=arguments["html"],
                agent_id=agent_name,
                owner_user_id=oauth2_user,
                pin=bool(arguments.get("pin", False)),
                ttl_days=arguments.get("ttl_days"),
            )
            return [
                {
                    "type": "text",
                    "text": (
                        f"Artifact created: {result['title']}\n"
                        f"URL: {result['url']}\n"
                        f"Expires: {result['expires_at']}"
                    ),
                }
            ]
        except Exception as err:
            _logger.exception("Failed to create artifact: %s", err)
            return [{"type": "text", "text": f"Error creating artifact: {err}"}]
