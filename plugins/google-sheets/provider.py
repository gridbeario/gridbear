"""Google Sheets MCP Provider Plugin.

Uses the mcp-google-sheets package (PyPI) with service account auth.
SA credentials managed by the google-sa plugin (vault key: svc:google-sa:credentials).
"""

import json
from pathlib import Path

from config.logging_config import logger
from core.interfaces.mcp_provider import BaseMCPProvider
from ui.secrets_manager import secrets_manager

# This server is a third-party package that imports mcp.server.fastmcp, a module
# mcp 2.x removed. It runs as its own process, so the image gives it a private
# virtualenv and its SDK version stops depending on ours. Falls back to PATH
# where that venv does not exist, such as a developer machine.
_ISOLATED_BIN = Path("/opt/mcp-google-sheets/bin/mcp-google-sheets")
GSHEETS_COMMAND = str(_ISOLATED_BIN) if _ISOLATED_BIN.exists() else "mcp-google-sheets"

SECRET_KEY = "svc:google-sa:credentials"

ALL_TOOLS = [
    "list_spreadsheets",
    "create_spreadsheet",
    "get_multiple_spreadsheet_summary",
    "get_sheet_data",
    "get_sheet_formulas",
    "update_cells",
    "batch_update_cells",
    "get_multiple_sheet_data",
    "list_sheets",
    "create_sheet",
    "rename_sheet",
    "copy_sheet",
    "add_rows",
    "add_columns",
    "share_spreadsheet",
    "find_in_spreadsheet",
    "list_folders",
    "search_spreadsheets",
    "batch_update",
]


class GoogleSheetsProvider(BaseMCPProvider):
    """Google Sheets MCP server provider using service account auth."""

    name = "google-sheets"

    def __init__(self, config: dict):
        super().__init__(config)
        self.drive_folder_id = config.get("drive_folder_id", "")
        self.enabled_tools = config.get("enabled_tools", "")

    async def initialize(self) -> None:
        if has_secret():
            logger.info("Google Sheets MCP provider initialized with SA")
        else:
            logger.warning(
                "Google Sheets MCP provider: no SA configured (add via admin UI)"
            )

    async def shutdown(self) -> None:
        pass

    def get_server_config(self) -> dict:
        env = {}

        # Global SA (optional — agent-level SAs may be used instead)
        sa_b64 = _get_sa_b64()
        if sa_b64:
            env["CREDENTIALS_CONFIG"] = sa_b64

        if self.drive_folder_id:
            env["DRIVE_FOLDER_ID"] = self.drive_folder_id

        if self.enabled_tools:
            env["ENABLED_TOOLS"] = self.enabled_tools

        if not sa_b64:
            logger.info("Google Sheets MCP: no global SA — agent-level SAs required")

        return {
            "google-sheets": {
                "command": GSHEETS_COMMAND,
                "args": [],
                "env": env,
            }
        }

    def get_user_server_config(self, unified_id: str, credentials: dict) -> dict:
        """Per-agent/user config with their own SA."""
        sa_b64 = credentials.get("sa_b64", "")
        env = {"CREDENTIALS_CONFIG": sa_b64}

        if self.drive_folder_id:
            env["DRIVE_FOLDER_ID"] = self.drive_folder_id

        if self.enabled_tools:
            env["ENABLED_TOOLS"] = self.enabled_tools

        return {
            "command": GSHEETS_COMMAND,
            "args": [],
            "env": env,
        }

    async def health_check(self) -> bool:
        return has_secret()

    def get_required_permissions(self) -> list[str]:
        return ["google-sheets"]

    def get_server_names(self) -> list[str]:
        return ["google-sheets"]

    def get_allowed_tools(self) -> list[str]:
        prefix = "mcp__google-sheets__"
        return [f"{prefix}{t}" for t in ALL_TOOLS]


# ── Secret helpers ─────────────────────────────────────────────────


def _get_sa_b64() -> str:
    """Get base64-encoded SA JSON from vault (empty string if not set)."""
    raw = secrets_manager.get_plain(SECRET_KEY)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return data.get("sa_b64", "")
    except (json.JSONDecodeError, TypeError):
        return raw


def has_secret() -> bool:
    """Check if SA exists in vault."""
    return bool(_get_sa_b64())
