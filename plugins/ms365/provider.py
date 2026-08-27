"""Microsoft 365 MCP Provider Plugin.

Provides MS365 MCP server configuration for multi-tenant access.
Tokens are stored via secrets_manager and passed via env var at runtime.
"""

import json
from pathlib import Path

from config.logging_config import logger
from core.interfaces.mcp_provider import BaseMCPProvider
from ui.secrets_manager import secrets_manager

from .context_service import MS365ContextService


class MS365Provider(BaseMCPProvider):
    """Microsoft 365 MCP server provider (multi-tenant)."""

    name = "ms365"

    def __init__(self, config: dict):
        super().__init__(config)
        self.server_path = Path(__file__).parent / "server.py"
        self.client_id = config.get("client_id", "")
        self.redirect_uri = config.get(
            "redirect_uri", "http://localhost:8088/plugins/ms365/callback"
        )
        self._server_names: list[str] = []
        self._context_service: MS365ContextService | None = None

    async def initialize(self) -> None:
        """Initialize provider and discover tenants."""
        if not self.client_id:
            logger.warning("MS365: client_id not configured, plugin disabled")
            return

        # Check client secret
        client_secret = secrets_manager.get("MS365_CLIENT_SECRET")
        if not client_secret:
            logger.warning("MS365: client secret not configured")

        # Discover configured tenants
        tenants = self._get_configured_tenants()
        self._server_names = [
            f"ms365-{t['name']}" for t in tenants if self._has_token(t["name"])
        ]

        # Initialize context service
        self._context_service = MS365ContextService(self.config)
        await self._context_service.initialize()

        logger.info(
            f"MS365 MCP provider initialized with {len(self._server_names)} authenticated tenants"
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._context_service:
            await self._context_service.shutdown()

    def _get_configured_tenants(self) -> list[dict]:
        """Get tenants from configuration."""
        return self.config.get("tenants", [])

    def _has_token(self, tenant_name: str) -> bool:
        """Check if token exists for tenant."""
        secret_key = f"ms365_token_{tenant_name}"
        return secrets_manager.get(secret_key) is not None

    def _get_token_for_tenant(self, tenant_name: str) -> dict | None:
        """Get decrypted OAuth token data for tenant.

        Returns None if token not found.
        """
        secret_key = f"ms365_token_{tenant_name}"
        token_json = secrets_manager.get_plain(secret_key)
        if not token_json:
            return None
        try:
            return json.loads(token_json)
        except json.JSONDecodeError:
            logger.error(f"Invalid token JSON for tenant {tenant_name}")
            return None

    def get_server_config(self) -> dict:
        """Get MCP server configurations for all authenticated tenants.

        Returns a dict mapping server names to their configurations.
        Token is passed via MS365_TOKEN_DATA environment variable.

        If azure_id is "auto" or "common", the tenant ID is auto-discovered
        from the stored token (set during OAuth callback).
        """
        client_secret = secrets_manager.get_plain("MS365_CLIENT_SECRET")
        tenants = self._get_configured_tenants()

        servers = {}
        for tenant in tenants:
            tenant_name = tenant["name"]
            token_data = self._get_token_for_tenant(tenant_name)
            if not token_data:
                continue

            # Auto-discover tenant ID from token if configured as "auto" or "common"
            configured_azure_id = tenant.get("azure_id", "common")
            if configured_azure_id in ("auto", "common"):
                # Use tenant ID discovered during OAuth (stored in token)
                tenant_id = token_data.get("azure_tenant_id", "common")
            else:
                tenant_id = configured_azure_id

            server_name = f"ms365-{tenant_name}"
            servers[server_name] = {
                "command": "python",
                "args": [str(self.server_path)],
                "env": {
                    "MS365_CLIENT_ID": self.client_id,
                    "MS365_CLIENT_SECRET": client_secret,
                    "MS365_TENANT_NAME": tenant_name,
                    "MS365_TENANT_ID": tenant_id,
                    "MS365_TOKEN_DATA": json.dumps(token_data),
                    "MS365_ROLE": tenant.get("role", "guest"),
                },
            }

        return servers

    async def health_check(self) -> bool:
        """Check if MS365 MCP server is available."""
        return self.server_path.exists() and bool(self.client_id)

    def get_required_permissions(self) -> list[str]:
        """Permissions required to use this provider.

        For MS365, each tenant requires its own permission.
        """
        return self._server_names

    def get_server_names(self) -> list[str]:
        """Get list of MCP server names this provider creates."""
        return self._server_names

    def get_allowed_tools(self) -> list[str]:
        """Get list of allowed tools for Claude CLI.

        Returns tools for all authenticated tenants in the format:
        mcp__{server_name}__{tool_name}
        """
        tool_names = [
            # SharePoint
            "m365_list_sites",
            "m365_get_site_by_url",
            "m365_list_files",
            "m365_read_file",
            "m365_write_file",
            "m365_search_files",
            # Planner
            "m365_list_groups",
            "m365_list_plans",
            "m365_get_plan_by_id",
            "m365_list_tasks",
            "m365_get_task",
            "m365_create_task",
            "m365_complete_task",
            # OneDrive
            "m365_list_drive_files",
            "m365_read_drive_file",
            "m365_write_drive_file",
            # Shared with me
            "m365_list_shared",
            "m365_read_shared",
        ]

        allowed = []
        for server_name in self._server_names:
            for tool in tool_names:
                allowed.append(f"mcp__{server_name}__{tool}")

        return allowed

    @staticmethod
    def store_token(tenant_name: str, token_data: dict) -> None:
        """Store OAuth token encrypted in secrets db.

        Args:
            tenant_name: MS365 tenant friendly name
            token_data: OAuth token data (access_token, refresh_token, etc.)
        """
        secret_key = f"ms365_token_{tenant_name}"
        secrets_manager.set(secret_key, json.dumps(token_data))
        logger.info(f"Stored encrypted token for tenant {tenant_name}")

    @staticmethod
    def delete_token(tenant_name: str) -> None:
        """Delete OAuth token from secrets db."""
        secret_key = f"ms365_token_{tenant_name}"
        secrets_manager.delete(secret_key)
        logger.info(f"Deleted token for tenant {tenant_name}")

    @staticmethod
    def has_token(tenant_name: str) -> bool:
        """Check if token exists for tenant."""
        secret_key = f"ms365_token_{tenant_name}"
        return secrets_manager.get(secret_key) is not None
