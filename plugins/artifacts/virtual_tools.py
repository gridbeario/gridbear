"""LocalToolProvider exposing the artifacts__create_artifact MCP tool.

Lives in a dedicated file (per the discover_local_tool_providers convention:
the manifest's `virtual_tools` key points to a module that contains a
LocalToolProvider subclass; the discoverer imports the file and instantiates
the class).
"""

from __future__ import annotations

import logging

from core.interfaces.local_tools import LocalToolProvider
from plugins.artifacts.service import ArtifactsService

_logger = logging.getLogger(__name__)

_SERVER_NAME = "artifacts"

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

        # TODO(v2): thread conversation_id through from the MCP gateway
        # (kwargs.get("conversation_id")) so /me/artifacts can filter by thread.
        # Current v1 leaves the field NULL for tool-created artifacts.

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
