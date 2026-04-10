"""Base tool adapter for runner plugins.

Handles MCP Gateway communication (JSON-RPC, auth, caching) so each
runner only implements the provider-specific conversion methods:
``convert_mcp_tools`` and ``format_tool_result``.
"""

import os
from itertools import count

import aiohttp

from config.logging_config import logger


class BaseToolAdapter:
    """MCP Gateway client with auth, caching, and retry.

    Subclasses must implement:
    - A ``convert_mcp_tools`` method (name varies per provider)
    - A ``format_tool_result`` method (signature varies per provider)

    Optionally override ``_resolve_tool_name`` for name sanitisation
    (e.g. Gemini's hyphen→underscore mapping).
    """

    _request_id = count(1)

    def __init__(self, gateway_url: str | None = None):
        self._gateway_url = gateway_url or os.getenv(
            "MCP_GATEWAY_URL", "http://localhost:8000"
        )
        self._session: aiohttp.ClientSession | None = None
        self._agent_id: str | None = None
        self._unified_id: str | None = None
        self._token: str | None = None
        self._headers: dict[str, str] = {}
        self._tools_cache: list[dict] | None = None

    async def initialize(self, agent_id: str, unified_id: str | None = None) -> None:
        """Set up the HTTP session with agent's MCP token.

        Args:
            agent_id: Agent identifier for token lookup.
            unified_id: Optional user identity for user-aware MCP servers.
                When set, included as ``user_identity`` in every JSON-RPC
                request so the gateway can connect to user-aware servers
                (e.g. Odoo) with the correct per-user credentials.
        """
        if self._session and not self._session.closed:
            await self._session.close()

        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        self._agent_id = agent_id
        self._unified_id = unified_id
        self._token = tm.get_token(agent_id)
        if not self._token:
            logger.warning("[%s] No MCP token available — tools disabled", agent_id)
            return
        self._headers = {"Authorization": f"Bearer {self._token}"}
        self._session = aiohttp.ClientSession()
        self._tools_cache = None

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request to the MCP Gateway.

        Handles 401 by refreshing the token and retrying once.
        """
        if not self._session or self._session.closed:
            return {"error": {"message": "ToolAdapter not initialized"}}

        # Inject user identity so the gateway can use per-user credentials
        # for user-aware servers (e.g. Odoo)
        if self._unified_id:
            params = {**params, "user_identity": self._unified_id}

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._request_id),
            "method": method,
            "params": params,
        }

        try:
            async with self._session.post(
                f"{self._gateway_url}/mcp",
                json=payload,
                headers=self._headers,
            ) as resp:
                if resp.status == 401 and self._agent_id:
                    return await self._retry_with_fresh_token(payload)
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error("MCP Gateway request failed: %s", e)
            return {"error": {"message": str(e)}}

    async def _retry_with_fresh_token(self, payload: dict) -> dict:
        """Refresh the MCP token and retry the request once."""
        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        self._token = tm.get_token(self._agent_id)
        if not self._token:
            return {"error": {"message": "Token refresh failed"}}
        self._headers = {"Authorization": f"Bearer {self._token}"}

        try:
            async with self._session.post(
                f"{self._gateway_url}/mcp",
                json=payload,
                headers=self._headers,
            ) as retry_resp:
                return await retry_resp.json()
        except aiohttp.ClientError as e:
            return {"error": {"message": f"Retry failed: {e}"}}

    async def list_tools(
        self,
        tool_budget: int | None = None,
        tool_loading: str = "full",
    ) -> list[dict]:
        """Fetch available tools from the MCP Gateway.

        Args:
            tool_budget: Optional max MCP tools the gateway should return.
                Passed as ``tool_budget`` in the JSON-RPC request.
            tool_loading: "full" (all tools) or "search" (built-in only).
                Passed as ``tool_loading`` in the JSON-RPC request.
        """
        if self._tools_cache is not None:
            return self._tools_cache

        params: dict = {}
        if tool_budget:
            params["tool_budget"] = tool_budget
        if tool_loading and tool_loading != "full":
            params["tool_loading"] = tool_loading
        data = await self._request("tools/list", params)
        if "error" in data:
            logger.warning("tools/list error: %s", data["error"])
            return []

        tools = data.get("result", {}).get("tools", [])
        self._tools_cache = tools
        return tools

    def invalidate_cache(self) -> None:
        """Clear the tools cache (e.g. between calls)."""
        self._tools_cache = None

    def _resolve_tool_name(self, name: str) -> str:
        """Map a provider-side tool name back to the MCP original.

        Override in subclasses that sanitise names (e.g. Gemini).
        Default is identity — name passes through unchanged.
        """
        return name

    async def call_tool(self, name: str, arguments: dict) -> list[dict]:
        """Execute a tool call via the MCP Gateway."""
        original_name = self._resolve_tool_name(name)
        data = await self._request(
            "tools/call", {"name": original_name, "arguments": arguments}
        )
        if "error" in data:
            error_msg = data["error"].get("message", "Unknown error")
            logger.error("tools/call error for %s: %s", name, error_msg)
            return [{"type": "text", "text": f"Tool error: {error_msg}"}]

        return data.get("result", {}).get("content", [])

    @staticmethod
    def _extract_text_parts(content: list[dict]) -> list[str]:
        """Extract text from MCP response content parts."""
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image":
                text_parts.append("[image content]")
            elif part.get("type") == "resource":
                text_parts.append(part.get("text", "[resource]"))
        return text_parts

    async def shutdown(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._tools_cache = None
