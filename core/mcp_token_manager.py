"""MCP Token Manager — provisions OAuth2 tokens for agents to access the MCP Gateway.

Each agent gets a dedicated OAuth2 client with mcp_permissions matching its config.
The token is used to authenticate SSE connections to the gateway.
"""

import json
import logging
import re

from config.settings import DATA_DIR
from core.oauth2.models import OAuth2Database

_MCP_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")

logger = logging.getLogger(__name__)


class MCPTokenManager:
    """Manages OAuth2 client/token provisioning for MCP gateway access."""

    GATEWAY_SERVER_NAME = "gridbear-gateway"

    def __init__(self, oauth2_db: OAuth2Database, gateway_url: str):
        self.oauth2_db = oauth2_db
        self.gateway_url = gateway_url.rstrip("/")
        self._agent_tokens: dict[str, str] = {}
        self._agent_secrets: dict[str, str] = {}
        self._agent_mcp_permissions: dict[str, list[str]] = {}
        self._data_dir = DATA_DIR
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def provision_agent_tokens(self, agents: list) -> None:
        """For each agent, create/find OAuth2 client and generate token.

        Args:
            agents: List of Agent objects with .name and .config.mcp_permissions
        """
        for agent in agents:
            try:
                self._provision_single_agent(agent)
            except Exception as e:
                logger.error(
                    f"Failed to provision MCP token for agent {agent.name}: {e}"
                )

    def _provision_single_agent(self, agent) -> None:
        """Provision OAuth2 client and token for a single agent."""
        agent_name = agent.name
        mcp_permissions = agent.config.mcp_permissions or []

        if not mcp_permissions:
            logger.debug(
                f"Agent {agent_name} has no MCP permissions, skipping token provisioning"
            )
            return

        self._agent_mcp_permissions[agent_name] = mcp_permissions

        # Find existing client for this agent
        client = self.oauth2_db.get_by_agent_name(agent_name)

        if client:
            # Update mcp_permissions if changed
            current_perms = client.get_mcp_permissions_list() or []
            if sorted(current_perms) != sorted(mcp_permissions):
                self.oauth2_db.update_client(client.id, mcp_permissions=mcp_permissions)
                logger.info(
                    f"Updated MCP permissions for agent {agent_name}: {mcp_permissions}"
                )

            # Regenerate secret so we can create a fresh token
            plain_secret = self.oauth2_db.regenerate_secret(client.id)
            if plain_secret:
                self._agent_secrets[agent_name] = plain_secret
            else:
                logger.error(f"Failed to regenerate secret for agent {agent_name}")
                return
        else:
            # Create new client
            client, plain_secret = self.oauth2_db.create_client(
                name=f"GridBear Agent - {agent_name}",
                client_type="confidential",
                agent_name=agent_name,
                mcp_permissions=mcp_permissions,
                access_token_expiry=86400,  # 24h
                require_pkce=False,
                allowed_scopes="mcp",
            )
            if plain_secret:
                self._agent_secrets[agent_name] = plain_secret
            logger.info(
                f"Created OAuth2 client for agent {agent_name} (client_id={client.client_id})"
            )

        # Create access token directly via DB (30 days — regenerated at each startup)
        token_obj = self.oauth2_db.create_access_token(
            client_pk=client.id,
            scope="mcp",
            access_expiry=30 * 86400,
            include_refresh=False,
        )

        self._agent_tokens[agent_name] = token_obj.token
        logger.info(f"Provisioned MCP gateway token for agent {agent_name}")

        # Write static MCP config file for this agent
        self._write_agent_mcp_config(agent_name, token_obj.token)

    def get_token(self, agent_name: str) -> str | None:
        """Get the current token for an agent."""
        return self._agent_tokens.get(agent_name)

    def get_config_path(self, agent_name: str) -> str | None:
        """Get the MCP config file path for an agent, if provisioned."""
        if agent_name not in self._agent_tokens:
            return None
        path = self._data_dir / f"mcp_agent_{agent_name}.json"
        if path.exists():
            return str(path)
        return None

    def get_allowed_tools(self, agent_name: str) -> list[str]:
        """Return the Claude-CLI allowlist pattern covering this gateway.

        Claude Code's permission matcher treats an MCP tool name as three
        double-underscore-separated parts: ``mcp__<server>__<tool>``. The
        tool segment is a single unit even when it contains ``__`` itself
        (e.g. ``artifacts__create_artifact`` or ``dwh_doreca__list_tables``).
        A pattern with four segments (``mcp__<gw>__<sub>__*``) therefore
        never matches the CLI's three-segment view of the tool name, and
        every tool call fires the manual-approval popup regardless of
        ``mcp_permissions``.

        The right pattern is the three-segment form ``mcp__<gateway>__*``
        which accepts every tool the gateway exposes. Per-server and
        per-user ACL is still authoritative — it's enforced server-side
        inside the gateway against the agent's bearer token (oauth2
        client permissions). The client-side glob is only a coarse
        outer fence; the fine filter is already where it belongs.
        """
        if agent_name not in self._agent_tokens:
            return []
        perms = self._agent_mcp_permissions.get(agent_name, [])
        if not perms:
            return []
        return [f"mcp__{self.GATEWAY_SERVER_NAME}__*"]

    def _write_agent_mcp_config(self, agent_name: str, token: str) -> None:
        """Write static MCP config file for an agent."""
        config = {
            "mcpServers": {
                "gridbear-gateway": {
                    "type": "http",
                    "url": f"{self.gateway_url}/mcp",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }

        config_path = self._data_dir / f"mcp_agent_{agent_name}.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        logger.debug(f"Wrote MCP config for agent {agent_name}: {config_path}")

    def cleanup(self) -> None:
        """Remove temporary MCP config files."""
        for agent_name in self._agent_tokens:
            config_path = self._data_dir / f"mcp_agent_{agent_name}.json"
            if config_path.exists():
                try:
                    config_path.unlink()
                except Exception:
                    pass


# Global instance
_token_manager: MCPTokenManager | None = None


def get_mcp_token_manager() -> MCPTokenManager | None:
    """Get the global MCP token manager instance."""
    return _token_manager


def set_mcp_token_manager(tm: MCPTokenManager) -> None:
    """Set the global MCP token manager instance."""
    global _token_manager
    _token_manager = tm
