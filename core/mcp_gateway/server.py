"""MCP Gateway - Streamable HTTP Transport.

Implements the MCP Streamable HTTP transport protocol:
- POST /mcp: Client sends JSON-RPC messages, server responds
- GET /mcp: Client opens SSE stream for server notifications
- DELETE /mcp: Client closes session

Authentication: Bearer token (OAuth2) required on all endpoints.
Returns 401 with resource_metadata URL to trigger OAuth flow.

Tool aggregation: Dynamically discovers and proxies tools from all
active MCP providers (Phase 2).
"""

import os
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from config.logging_config import logger
from core.api_schemas import api_error, api_ok
from core.encryption import decrypt, is_encrypted

router = APIRouter()

MCP_SERVER_INFO = {
    "name": "gridbear-mcp-gateway",
    "version": "0.2.0",
}

MCP_CAPABILITIES = {
    "tools": {"listChanged": False},
}

# Active sessions: session_id -> {client info}
_sessions: dict[str, dict] = {}

# Side-channel user context: agent_name -> unified_id
# Set by CLI runner via POST /mcp/user-context before each prompt.
_agent_user_context: dict[str, str | None] = {}

# Client manager reference (set by admin/app.py at startup)
_client_manager = None

# Last provider refresh timestamp
_last_refresh: float = 0
_REFRESH_INTERVAL = int(os.getenv("MCP_REFRESH_INTERVAL", "30"))

# Tools list cache: (timestamp, tools_list) per cache_key
_tools_cache: dict[str, tuple[float, list[dict]]] = {}
_TOOLS_CACHE_TTL = int(os.getenv("MCP_TOOLS_CACHE_TTL", "120"))  # seconds

# Rate limiting
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("MCP_RATE_LIMIT_MAX_REQUESTS", "100"))
_RATE_LIMIT_WINDOW = float(os.getenv("MCP_RATE_LIMIT_WINDOW", "60"))
_MAX_SSE_CONNECTIONS = int(os.getenv("MCP_MAX_SSE_CONNECTIONS", "5"))
_SSE_KEEPALIVE_INTERVAL = int(os.getenv("MCP_SSE_KEEPALIVE_INTERVAL", "30"))


class RateLimiter:
    """In-memory sliding window rate limiter."""

    def __init__(self):
        # key -> list of timestamps
        self._windows: dict[str, list[float]] = {}

    def check(self, key: str, max_requests: int, window_seconds: float) -> float | None:
        """Check if request is allowed.

        Returns None if allowed, or seconds until next allowed request.
        """
        now = time.time()
        cutoff = now - window_seconds

        if key not in self._windows:
            self._windows[key] = []

        # Prune old entries
        timestamps = self._windows[key]
        self._windows[key] = timestamps = [t for t in timestamps if t >= cutoff]

        if len(timestamps) >= max_requests:
            oldest = timestamps[0]
            return (oldest + window_seconds) - now

        timestamps.append(now)
        return None

    def count_active(self, prefix: str) -> int:
        """Count keys with given prefix that have recent entries (for SSE connections)."""
        now = time.time()
        count = 0
        for key, timestamps in self._windows.items():
            if key.startswith(prefix) and timestamps and (now - timestamps[-1]) < 60:
                count += 1
        return count

    def cleanup(self) -> None:
        """Remove stale entries older than 5 minutes."""
        cutoff = time.time() - 300
        stale = [k for k, v in self._windows.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._windows[k]


# Global rate limiter
_rate_limiter = RateLimiter()


def set_client_manager(manager) -> None:
    """Set the MCPClientManager instance. Called from admin/app.py at startup."""
    global _client_manager
    _client_manager = manager


def get_client_manager():
    """Get the MCPClientManager instance."""
    return _client_manager


# Local tool providers (registered by plugins via set_local_tool_providers)
_local_tool_providers: list = []

# Async task manager reference (set by admin/app.py at startup)
_task_manager = None


def set_task_manager(manager) -> None:
    """Set the AsyncTaskManager instance. Called from admin/app.py at startup."""
    global _task_manager
    _task_manager = manager


def get_task_manager():
    """Get the AsyncTaskManager instance."""
    return _task_manager


def set_local_tool_providers(providers: list) -> None:
    """Set the list of LocalToolProvider instances.

    Called from admin/app.py at startup after discovering plugins that
    implement LocalToolProvider.
    """
    global _local_tool_providers
    _local_tool_providers = providers


def get_local_tool_providers() -> list:
    """Get the list of LocalToolProvider instances."""
    return _local_tool_providers


def _get_base_url(request: Request) -> str:
    """Get external base URL (respects GRIDBEAR_BASE_URL env var for proxied setups)."""
    base = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base


def _check_bearer(request: Request) -> Response | None:
    """Validate Bearer token. Returns error Response or None if valid."""
    auth_header = request.headers.get("authorization", "")

    if not auth_header.startswith("Bearer "):
        base_url = _get_base_url(request)
        resource_url = f"{base_url}/.well-known/oauth-protected-resource"
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_token",
                "error_description": "Bearer token required",
            },
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_url}"'},
        )

    token_string = auth_header[7:]

    from core.oauth2.server import get_db

    db = get_db()
    token, client = db.validate_token(token_string)

    if not token:
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_token",
                "error_description": "Invalid or expired token",
            },
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )

    # Attach token info to request state
    request.state.oauth2_token = token
    request.state.oauth2_client = client
    request.state.oauth2_user = token.user_identity
    request.state.oauth2_scope = token.scope
    if client:
        request.state.oauth2_mcp_permissions = client.get_mcp_permissions_list()

    # Update last used (best effort)
    ip = request.client.host if request.client else None
    db.update_last_used(token.id, ip_address=ip)

    return None


def _jsonrpc_response(id, result):
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(id, code, message):
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def _filter_by_permissions(tools: list[dict], mcp_permissions: list[str]) -> list[dict]:
    """Filter tools by OAuth2 client MCP permissions."""
    from core.permissions.mcp_resolver import filter_tools_by_permissions

    reverse_map = _client_manager._sanitized_to_original if _client_manager else {}
    return filter_tools_by_permissions(
        tools,
        mcp_permissions,
        sanitized_to_original=reverse_map,
    )


def _filter_by_user_mcp_permissions(tools: list[dict], unified_id: str) -> list[dict]:
    """Filter tools by per-user MCP permissions (set in admin /permissions page).

    If the user has no explicit permissions, all tools pass through (agent-level
    mcp_permissions has already been enforced upstream). If the user has
    permissions, only namespaced tools from allowed servers are kept.
    Non-namespaced tools (gridbear_help, send_file_to_chat, etc.) always pass
    through. System callers (inter-agent, workflow) bypass user-level filtering
    — they rely on agent-level mcp_permissions instead.
    """
    # System callers are not real users — skip per-user filtering
    if unified_id and unified_id.startswith("inter_agent:"):
        return tools

    try:
        from config.settings import get_user_mcp_permissions

        user_perms = get_user_mcp_permissions(unified_id)
        if not user_perms:
            # No per-user perms configured → defer entirely to the agent-level
            # filter that has already run on this list. Returning only the
            # non-namespaced subset (the previous behaviour) silently killed
            # every MCP tool on any deploy that hadn't hand-populated
            # app.user_mcp_permissions — directly contradicting the docstring
            # contract this function advertises.
            return tools
        pre = len(tools)
        reverse_map = _client_manager._sanitized_to_original if _client_manager else {}
        from core.permissions.mcp_resolver import matches_permission

        ns_sep = "__"
        filtered = []
        for tool in tools:
            name = tool.get("name", "")
            if ns_sep in name:
                sanitized = name[: name.index(ns_sep)]
                original = reverse_map.get(sanitized, sanitized)
                if matches_permission(original, user_perms):
                    filtered.append(tool)
            else:
                # Non-namespaced = built-in system tools, always allowed
                filtered.append(tool)
        if len(filtered) < pre:
            logger.info(
                "MCP Gateway: user %s perms filtered %d→%d (user_perms=%s)",
                unified_id,
                pre,
                len(filtered),
                user_perms,
            )
        return filtered
    except Exception as e:
        logger.warning("Error filtering by user MCP permissions: %s", e)
        return tools


def _filter_by_user_prefs(tools: list[dict], unified_id: str) -> list[dict]:
    """Filter out tools the user has explicitly disabled."""
    try:
        from ui.auth.database import get_auth_db

        disabled = get_auth_db().get_disabled_tools(unified_id)
        if not disabled:
            return tools
        return [t for t in tools if t.get("name", "") not in disabled]
    except Exception:
        return tools


def _filter_by_agent_prefs(tools: list[dict], agent_name: str) -> list[dict]:
    """Filter out tools the admin has disabled for this agent."""
    try:
        from ui.auth.database import get_auth_db

        disabled = get_auth_db().get_agent_disabled_tools(agent_name)
        if not disabled:
            return tools
        return [t for t in tools if t.get("name", "") not in disabled]
    except Exception:
        return tools


# Agent config cache (populated on first access, cleared on provider refresh)
_agent_config_cache: dict[str, tuple[float, dict]] = {}
_AGENT_CONFIG_TTL = 10  # seconds


def _load_agent_config(agent_name: str) -> dict | None:
    """Load agent config from DB with short TTL cache.

    Used as a fallback when the runner doesn't send tool_loading/max_tools
    in the JSON-RPC params (e.g. Claude CLI process pool).
    """
    import time

    now = time.time()
    cached = _agent_config_cache.get(agent_name)
    if cached and (now - cached[0]) < _AGENT_CONFIG_TTL:
        return cached[1]

    from core.models.agent_config import AgentConfigRecord

    record = AgentConfigRecord.get_sync(id=agent_name)
    if not record:
        return None

    config = dict(record)
    _agent_config_cache[agent_name] = (now, config)
    return config


def invalidate_agent_config_cache(agent_name: str | None = None) -> None:
    """Invalidate agent config cache. Called on reload.

    Also drops any ``_tools_cache`` entries tied to the agent so that
    ``mcp_permissions``, ``service_account``, ``max_tools`` or other
    changes land immediately instead of waiting for the 120s TTL to
    expire.
    """
    if agent_name:
        _agent_config_cache.pop(agent_name, None)
        _invalidate_tools_cache(agent_name)
    else:
        _agent_config_cache.clear()
        _tools_cache.clear()


def _invalidate_tools_cache(agent_name: str) -> None:
    """Drop ``_tools_cache`` entries whose key targets ``agent_name``.

    Cache keys are shaped as
    ``"<agent>:<user>:<tool_loading>:<tool_budget>[:<perms>]"``; keys
    belonging to ``agent_name`` therefore share the ``"<agent>:"``
    prefix. A full prefix match on ``"<agent>:"`` is enough — no need
    to parse the rest of the key.
    """
    prefix = f"{agent_name}:"
    stale = [k for k in _tools_cache if k.startswith(prefix)]
    for k in stale:
        _tools_cache.pop(k, None)
    if stale:
        logger.debug(
            "Dropped %d stale tools_cache entries for agent %s",
            len(stale),
            agent_name,
        )


# Server category mapping (populated after provider refresh)
_server_categories: dict[str, str] = {}


def _build_server_categories() -> None:
    """Build server_name -> category mapping from ServerInfo."""
    _agent_config_cache.clear()  # also invalidate agent config on refresh
    if not _client_manager:
        return
    _server_categories.clear()
    for name, info in _client_manager._known_servers.items():
        _server_categories[name] = info.category


def _get_server_category(server_prefix: str) -> str:
    """Get category for a server by its prefix (tool name prefix)."""
    return _server_categories.get(server_prefix, "system")


# Prefixes for built-in tools (always outside the tool budget)
_BUILTIN_PREFIXES = (
    "gridbear_",
    "send_file",
    "ask_agent",
    "async_",
    "chat_history__",
    "conversation_docs__",
    "credential_vault__",
    "search_tools",
    "execute_discovered_tool",
)


def _apply_tool_budget(
    tools: list[dict],
    budget: int,
    agent_name: str | None = None,
) -> list[dict]:
    """Truncate MCP tool list respecting the budget.

    Built-in tools are OUTSIDE the budget (always included). The budget
    counts only MCP tools. Selection uses round-robin across servers for
    fair distribution.
    """
    builtin = [t for t in tools if t["name"].startswith(_BUILTIN_PREFIXES)]
    mcp = [t for t in tools if not t["name"].startswith(_BUILTIN_PREFIXES)]

    if budget <= 0:
        logger.warning(
            "Tool budget is %d for %s — returning only built-in tools",
            budget,
            agent_name,
        )
        return builtin

    if len(mcp) <= budget:
        return builtin + mcp

    # Round-robin per server for fair distribution
    by_server: dict[str, list[dict]] = {}
    for tool in mcp:
        server = tool["name"].split("__", 1)[0] if "__" in tool["name"] else "_virtual"
        by_server.setdefault(server, []).append(tool)

    selected: list[dict] = []
    server_names = sorted(by_server.keys())
    idx = 0
    while len(selected) < budget and server_names:
        server = server_names[idx % len(server_names)]
        if by_server[server]:
            selected.append(by_server[server].pop(0))
        else:
            server_names.remove(server)
            if not server_names:
                break
            continue
        idx += 1

    return builtin + selected


# gridbear_help meta-tool definition
_HELP_TOOL = {
    "name": "gridbear_help",
    "description": (
        "List available service categories and their tools. "
        "Call with no arguments to see categories, "
        "or with a category name to see its tools."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Category name to get tool details for (optional)",
            }
        },
    },
}


# --- Credential Vault virtual tools ----------------------------------------

_VAULT_SERVER_NAME = "credential-vault"

_VAULT_USER_PARAM = {
    "user_id": {
        "type": "string",
        "description": (
            "The user's platform username (e.g. Telegram @username without @, "
            "or Discord username). Do NOT use numeric chat IDs."
        ),
    },
}

_VAULT_LIST_TOOL = {
    "name": "credential_vault__list_services",
    "description": (
        "List all credential-vault services for a user. Returns service names and IDs."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {**_VAULT_USER_PARAM},
        "required": ["user_id"],
    },
}

_VAULT_INFO_TOOL = {
    "name": "credential_vault__get_service_info",
    "description": (
        "Get details for a credential-vault service: URL, notes, and "
        "credential keys (secret values are masked). Use this to understand "
        "which fields are available before calling fill_credentials."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            **_VAULT_USER_PARAM,
            "service_id": {
                "type": "string",
                "description": "The service identifier (from list_services)",
            },
        },
        "required": ["user_id", "service_id"],
    },
}

_VAULT_FILL_TOOL = {
    "name": "credential_vault__fill_credentials",
    "description": (
        "Fill form fields with credentials from the vault. "
        "Map each Playwright element ref to a credential key. "
        "Secret values are injected directly into the browser — "
        "they never appear in this conversation."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            **_VAULT_USER_PARAM,
            "service_id": {
                "type": "string",
                "description": "The service identifier",
            },
            "fields": {
                "type": "array",
                "description": "List of field mappings",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "Playwright element ref from browser snapshot",
                        },
                        "credential_key": {
                            "type": "string",
                            "description": "Name of the credential (e.g. 'username', 'password')",
                        },
                        "type": {
                            "type": "string",
                            "description": "Form field type (default: textbox)",
                            "enum": [
                                "textbox",
                                "checkbox",
                                "radio",
                                "combobox",
                                "slider",
                            ],
                            "default": "textbox",
                        },
                    },
                    "required": ["ref", "credential_key"],
                },
            },
        },
        "required": ["user_id", "service_id", "fields"],
    },
}

_VAULT_TOOLS = [_VAULT_LIST_TOOL, _VAULT_INFO_TOOL, _VAULT_FILL_TOOL]


# send_file_to_chat virtual tool definition
_SEND_FILE_TOOL = {
    "name": "send_file_to_chat",
    "description": (
        "Send a file (screenshot, document, image) to the user's chat. "
        "Use this after taking a screenshot or generating a file that the user "
        "should see. The file must exist at the specified path under /app/data/."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "Absolute path to the file to send "
                    "(e.g. /app/data/playwright/page-xxx.png)"
                ),
            },
            "chat_id": {
                "type": "string",
                "description": (
                    "The user's chat ID on the platform (e.g. Telegram numeric chat_id)"
                ),
            },
            "platform": {
                "type": "string",
                "description": "The messaging platform",
            },
            "caption": {
                "type": "string",
                "description": "Optional caption to accompany the file",
            },
        },
        "required": ["file_path", "chat_id", "platform"],
    },
}

_ASK_AGENT_TOOL = {
    "name": "ask_agent",
    "description": (
        "Send a message to another agent and get their response. "
        "Use this to delegate tasks or get information from specialized agents."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Target agent name/ID (see available agents in system prompt)",
            },
            "message": {
                "type": "string",
                "description": "Message to send to the agent",
            },
        },
        "required": ["agent_id", "message"],
    },
}


async def _handle_ask_agent(
    arguments: dict,
    agent_name: str | None,
    oauth2_user: str | None,
    mcp_permissions: list[str] | None = None,
) -> list[dict]:
    """Handle ask_agent: send message to another agent."""
    from core.registry import get_agent_manager

    agent_manager = get_agent_manager()
    if not agent_manager:
        return [{"type": "text", "text": "Error: agent manager not available."}]

    if not agent_name:
        return [{"type": "text", "text": "Error: could not resolve calling agent."}]

    target = arguments.get("agent_id", "").strip().lower()
    message = arguments.get("message", "")

    if not target or not message:
        return [{"type": "text", "text": "Error: agent_id and message are required."}]

    if target == agent_name:
        return [{"type": "text", "text": "Error: cannot send message to yourself."}]

    try:
        response = await agent_manager.send_inter_agent_message(
            from_agent=agent_name,
            to_agent=target,
            message=message,
            context={
                "source": "inter_agent",
                "from_agent": agent_name,
                "original_user_id": oauth2_user,
                "inter_agent_depth": 0,
                "mcp_permissions": mcp_permissions,
            },
        )
        return [{"type": "text", "text": response}]
    except Exception as e:
        return [{"type": "text", "text": f"Error: {e}"}]


async def _handle_vault_call(
    tool_name: str,
    arguments: dict,
) -> list[dict]:
    """Handle credential_vault__* tool calls locally."""
    from core import credential_vault

    raw_user_id = arguments.get("user_id")
    if not raw_user_id:
        return [
            {"type": "text", "text": "Error: user_id is required for vault operations."}
        ]
    user_id = credential_vault.resolve_user_id(raw_user_id)
    logger.debug("Vault: resolved user_id %r → %r", raw_user_id, user_id)

    short = tool_name.split("__", 1)[-1]  # strip namespace

    if short == "list_services":
        services = credential_vault.list_services(user_id)
        if not services:
            return [
                {
                    "type": "text",
                    "text": "No credential-vault services configured for this user.",
                }
            ]
        lines = ["## Credential Vault Services\n"]
        for svc in services:
            n_creds = len(svc.get("credentials", []))
            lines.append(
                f"- **{svc['service_id']}**: {svc['name']} ({n_creds} credentials)"
            )
        return [{"type": "text", "text": "\n".join(lines)}]

    if short == "get_service_info":
        service_id = arguments.get("service_id", "")
        entry = credential_vault.get_service(user_id, service_id)
        if entry is None:
            return [{"type": "text", "text": f"Service '{service_id}' not found."}]
        safe = entry.to_safe_dict()
        lines = [f"## {safe['name']}\n"]
        if safe["url"]:
            lines.append(f"**URL:** {safe['url']}")
        if safe["notes"]:
            lines.append(f"**Notes:** {safe['notes']}")
        lines.append("\n**Credentials:**")
        for c in safe["credentials"]:
            lines.append(f"- `{c['key']}`: {c['value']}")
        return [{"type": "text", "text": "\n".join(lines)}]

    if short == "fill_credentials":
        service_id = arguments.get("service_id", "")
        fields = arguments.get("fields", [])
        if not fields:
            return [{"type": "text", "text": "Error: no fields provided."}]
        entry = credential_vault.get_service(user_id, service_id)
        if entry is None:
            return [{"type": "text", "text": f"Service '{service_id}' not found."}]

        # Build credential lookup
        cred_map = {c.key: c.value for c in entry.credentials}

        # Build fill_form fields array
        fill_fields = []
        for f in fields:
            cred_key = f.get("credential_key", "")
            if cred_key not in cred_map:
                return [
                    {
                        "type": "text",
                        "text": f"Error: credential key '{cred_key}' not found in service '{service_id}'.",
                    }
                ]
            fill_fields.append(
                {
                    "ref": f["ref"],
                    "name": cred_key,
                    "type": f.get("type", "textbox"),
                    "value": cred_map[cred_key],
                }
            )

        # Dispatch to Playwright via client_manager
        if not _client_manager:
            return [
                {"type": "text", "text": "Error: MCP client manager not available."}
            ]

        try:
            await _client_manager.call_tool(
                "playwright__browser_fill_form",
                {"fields": fill_fields},
            )
        except Exception as e:
            logger.error("Vault fill_credentials failed: %s", e)
            return [{"type": "text", "text": f"Error filling form: {e}"}]

        # Return ONLY confirmation — never the Playwright result
        return [
            {
                "type": "text",
                "text": f"Filled {len(fill_fields)} field(s) for {entry.name}.",
            }
        ]

    return [{"type": "text", "text": f"Unknown vault tool: {tool_name}"}]


# --- Async task tools (always available) ---

_ASYNC_RUN_TOOL = {
    "name": "async_run_tool",
    "description": (
        "Run any MCP tool as a background task. Returns immediately with a task_id. "
        "Use this for long-running operations (e.g. videomaker__record_web) that would "
        "otherwise timeout. You will be notified on the specified chat when the task "
        "completes or fails."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "The MCP tool name to run in background (e.g. 'videomaker__record_web')",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments to pass to the tool",
            },
            "notify_chat_id": {
                "type": "string",
                "description": "Chat ID where to send completion notification",
            },
            "notify_platform": {
                "type": "string",
                "description": "Platform for notification delivery",
            },
        },
        "required": ["tool_name", "arguments", "notify_chat_id", "notify_platform"],
    },
}

_ASYNC_STATUS_TOOL = {
    "name": "async_task_status",
    "description": "Check the status and result of a background async task.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID returned by async_run_tool",
            },
        },
        "required": ["task_id"],
    },
}

_ASYNC_LIST_TOOL = {
    "name": "async_list_tasks",
    "description": "List all background async tasks for the current agent.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Optional filter by status",
                "enum": ["running", "completed", "failed"],
            },
        },
    },
}

_ASYNC_TOOLS = [_ASYNC_RUN_TOOL, _ASYNC_STATUS_TOOL, _ASYNC_LIST_TOOL]

_CHAT_HISTORY_DB = None  # Removed: now using PostgreSQL via get_database()

_CHAT_HISTORY_TOOLS = [
    {
        "name": "chat_history__get_recent",
        "description": "Get recent chat messages for the current user. "
        "Use when you need context beyond pre-loaded history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "integer",
                    "description": "User ID from system prompt",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform from system prompt",
                },
                "limit": {
                    "type": "integer",
                    "description": "Messages to retrieve (default 20, max 50)",
                },
            },
            "required": ["user_id", "platform"],
        },
    },
    {
        "name": "chat_history__search",
        "description": "Search through past chat messages by keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "integer",
                    "description": "User ID from system prompt",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform from system prompt",
                },
                "query": {
                    "type": "string",
                    "description": "Search keywords",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10, max 30)",
                },
            },
            "required": ["user_id", "platform", "query"],
        },
    },
]


# --- Conversation Documents tools ---

_CONVERSATION_DOCS_TOOLS = [
    {
        "name": "conversation_docs__read",
        "description": (
            "Read the full text content of a document uploaded to the current "
            "conversation. The list of available documents is shown in the system "
            "prompt under [Conversation Documents]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Conversation ID from channel_metadata",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename of the document to read",
                },
            },
            "required": ["conversation_id", "filename"],
        },
    },
]


async def _handle_conversation_docs_call(
    tool_name: str,
    arguments: dict,
) -> list[dict]:
    """Handle conversation_docs__* virtual tool calls."""
    from core.registry import get_database

    db = get_database()
    if db is None:
        return [{"type": "text", "text": "Database not available."}]

    short = tool_name.split("__", 1)[-1]

    if short == "read":
        conv_id = arguments.get("conversation_id", "").strip()
        filename = arguments.get("filename", "").strip()
        if not conv_id or not filename:
            return [
                {
                    "type": "text",
                    "text": "Error: conversation_id and filename are required.",
                }
            ]

        try:
            async with db.acquire() as conn:
                row = await (
                    await conn.execute(
                        "SELECT original_filename, content_text "
                        "FROM chat.webchat_documents "
                        "WHERE conversation_id = %s "
                        "AND original_filename = %s",
                        (conv_id, filename),
                    )
                ).fetchone()
                if not row:
                    return [
                        {
                            "type": "text",
                            "text": f"Document '{filename}' not found in this conversation.",
                        }
                    ]
                text = row["content_text"] or "(no text content extracted)"
                return [
                    {
                        "type": "text",
                        "text": f"--- {row['original_filename']} ---\n{text}\n--- end ---",
                    }
                ]
        except Exception as e:
            logger.error("conversation_docs tool error: %s", e)
            return [{"type": "text", "text": f"Error: {e}"}]

    return [{"type": "text", "text": f"Unknown tool: {tool_name}"}]


async def _handle_send_file(
    arguments: dict,
    agent_name: str | None,
) -> list[dict]:
    """Handle send_file_to_chat virtual tool call."""
    file_path = arguments.get("file_path", "")
    chat_id = arguments.get("chat_id", "")
    platform = arguments.get("platform", "")
    caption = arguments.get("caption")

    if not file_path or not chat_id or not platform:
        return [
            {
                "type": "text",
                "text": "Error: file_path, chat_id, and platform are required.",
            }
        ]

    if not agent_name:
        return [
            {
                "type": "text",
                "text": "Error: could not resolve agent from OAuth2 token.",
            }
        ]

    # Security: resolve to prevent path traversal
    from pathlib import Path

    resolved = str(Path(file_path).resolve())
    if not resolved.startswith("/app/data/"):
        return [
            {"type": "text", "text": "Error: only files under /app/data/ can be sent."}
        ]

    logger.info(
        f"send_file_to_chat: agent={agent_name} platform={platform} file={file_path}"
    )

    # Webchat: deliver via WebSocket push (no bot channel adapter needed)
    if platform == "webchat":
        logger.info(f"send_file_to_chat: routing to webchat handler, uid={chat_id}")
        return await _handle_send_file_webchat(resolved, chat_id, caption)

    internal_url = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
    internal_secret = os.getenv("INTERNAL_API_SECRET", "")

    try:
        import httpx

        async with httpx.AsyncClient(timeout=30) as http_client:
            resp = await http_client.post(
                f"{internal_url}/api/send-file",
                json={
                    "agent_name": agent_name,
                    "platform": platform,
                    "chat_id": chat_id,
                    "file_path": resolved,
                    "caption": caption,
                },
                headers={"Authorization": f"Bearer {internal_secret}"},
            )
            result = resp.json()

        if result.get("ok"):
            return [{"type": "text", "text": f"File sent to {platform} chat."}]
        else:
            error = result.get("error", "Unknown error")
            logger.error(f"send_file_to_chat failed: agent={agent_name} error={error}")
            return [{"type": "text", "text": f"Failed to send file: {error}"}]

    except Exception as e:
        logger.error(f"send_file_to_chat exception: agent={agent_name} error={e}")
        return [{"type": "text", "text": f"Error sending file: {e}"}]


async def _handle_send_file_webchat(
    file_path: str,
    uid: str,
    caption: str | None,
) -> list[dict]:
    """Deliver a file to webchat user via WebSocket push."""
    import mimetypes
    import shutil
    from pathlib import Path

    logger.info(f"_handle_send_file_webchat: start uid={uid} file={file_path}")

    src = Path(file_path)
    if not src.exists():
        logger.error(f"_handle_send_file_webchat: file not found: {file_path}")
        return [{"type": "text", "text": f"Error: file not found: {file_path}"}]

    # Resolve uid from active WebSocket connections if runner passed
    # a numeric placeholder (e.g. "0") instead of the username.
    try:
        from ui.routes.ws_chat import _active_connections
    except ImportError:
        _active_connections = {}

    logger.info(
        f"_handle_send_file_webchat: active connections={list(_active_connections.keys())}"
    )

    if uid not in _active_connections:
        if uid == "0" and _active_connections:
            uid = next(iter(_active_connections))
            logger.warning(
                f"send_file_to_chat webchat: chat_id was '0', "
                f"resolved to {uid} (rebuild bot to fix)"
            )
        elif not _active_connections:
            logger.error("_handle_send_file_webchat: no connections active")
            return [{"type": "text", "text": "Error: no webchat user connected."}]
        else:
            logger.error(
                f"_handle_send_file_webchat: uid={uid} not in {list(_active_connections.keys())}"
            )
            return [
                {
                    "type": "text",
                    "text": f"Error: webchat user '{uid}' not connected.",
                }
            ]

    try:
        # Copy to outbound directory for the file-serving endpoint
        outbound_dir = Path("/app/data/attachments/webchat-outbound") / uid
        outbound_dir.mkdir(parents=True, exist_ok=True)
        dest = outbound_dir / src.name
        if dest.exists():
            import time

            dest = outbound_dir / f"{int(time.time())}_{src.name}"
        shutil.copy2(str(src), str(dest))
        logger.info(f"_handle_send_file_webchat: copied to {dest}")

        # Create a file-serving token
        from ui.routes.chat_api import create_file_token

        token = create_file_token(str(dest), uid)
        url = f"/me/chat/api/files/{token}"
        filename = src.name
        mimetype = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
        logger.info(f"_handle_send_file_webchat: token={token[:8]}... url={url}")

        # Push attachment event to user's active WebSocket
        from ui.routes.ws_chat import push_to_webchat

        event = {
            "type": "attachment",
            "url": url,
            "filename": filename,
            "mimetype": mimetype,
            "caption": caption,
        }
        delivered = await push_to_webchat(uid, event)
        logger.info(f"_handle_send_file_webchat: delivered={delivered}")

        # Return the URL to the runner so it can include the image
        # in its markdown response (rendered by marked.parse in the frontend).
        # Direct WebSocket push has concurrency issues with the NDJSON stream.
        return [
            {
                "type": "text",
                "text": (
                    f"File delivered. "
                    f"Include this in your response as a markdown image so the "
                    f"user can see it: ![{filename}]({url})"
                ),
            }
        ]
    except Exception as exc:
        logger.error(f"_handle_send_file_webchat: exception: {exc}", exc_info=True)
        return [{"type": "text", "text": f"Error delivering file: {exc}"}]


async def _handle_chat_history_call(
    tool_name: str,
    arguments: dict,
) -> list[dict]:
    """Handle chat_history__* virtual tool calls via PostgreSQL."""
    from core.registry import get_database

    db = get_database()
    if db is None:
        return [{"type": "text", "text": "Chat history database not available."}]

    user_id = arguments.get("user_id")
    platform = arguments.get("platform", "")
    if not user_id or not platform:
        return [{"type": "text", "text": "Error: user_id and platform are required."}]

    short = tool_name.split("__", 1)[-1]

    def _decrypt_content(raw: str) -> str:
        """Decrypt content if encrypted, otherwise return as-is."""
        return decrypt(raw) if is_encrypted(raw) else raw

    try:
        async with db.acquire() as conn:
            if short == "get_recent":
                limit = min(arguments.get("limit", 20), 50)
                rows = await (
                    await conn.execute(
                        "SELECT role, content, created_at FROM chat.chat_history "
                        "WHERE user_id = %s AND platform = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (user_id, platform, limit),
                    )
                ).fetchall()
                if not rows:
                    return [{"type": "text", "text": "No chat history found."}]
                rows = list(reversed(rows))
                lines = [f"## Recent messages ({len(rows)})\n"]
                for r in rows:
                    ts = str(r["created_at"] or "")[:16]
                    role = "User" if r["role"] == "user" else "Assistant"
                    content = _decrypt_content(r["content"])
                    lines.append(f"[{ts}] **{role}**: {content[:500]}")
                return [{"type": "text", "text": "\n".join(lines)}]

            if short == "search":
                query = arguments.get("query", "").strip()
                if not query:
                    return [{"type": "text", "text": "Error: query is required."}]
                limit = min(arguments.get("limit", 10), 30)
                # Fetch recent messages and search in Python (content is encrypted)
                fetch_limit = 500
                rows = await (
                    await conn.execute(
                        "SELECT role, content, created_at "
                        "FROM chat.chat_history "
                        "WHERE user_id = %s AND platform = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (user_id, platform, fetch_limit),
                    )
                ).fetchall()
                query_lower = query.lower()
                matches = []
                for r in rows:
                    content = _decrypt_content(r["content"])
                    if query_lower in content.lower():
                        matches.append((r, content))
                        if len(matches) >= limit:
                            break
                if not matches:
                    return [
                        {"type": "text", "text": f"No messages matching '{query}'."}
                    ]
                lines = [f"## Search: '{query}' ({len(matches)} results)\n"]
                for r, content in matches:
                    ts = str(r["created_at"] or "")[:16]
                    role = "User" if r["role"] == "user" else "Assistant"
                    lines.append(f"[{ts}] **{role}**: {content[:300]}")
                return [{"type": "text", "text": "\n".join(lines)}]

    except Exception as e:
        logger.error("chat_history tool error: %s", e)
        return [{"type": "text", "text": f"Error: {e}"}]

    return [{"type": "text", "text": f"Unknown tool: {tool_name}"}]


# --- search_tools + execute_discovered_tool ---

_SEARCH_TOOLS_TOOL = {
    "name": "search_tools",
    "description": (
        "Search for available MCP tools by keyword. Use this to discover tools "
        "for a specific task. Returns matching tools with name, description, "
        "and category. After finding a tool, call execute_discovered_tool to use it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (e.g. 'invoice', 'email', 'light')",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category filter "
                    "(e.g. 'erp', 'communication', 'automation', 'development')"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (default 10)",
            },
        },
        "required": ["query"],
    },
}

_EXECUTE_DISCOVERED_TOOL = {
    "name": "execute_discovered_tool",
    "description": (
        "Execute a tool that was previously found via search_tools. "
        "You MUST call search_tools first before using this tool. "
        "Pass the exact tool_name from search_tools results AND its required "
        "arguments in the 'arguments' object. "
        "CRITICAL: The 'arguments' field MUST contain ALL required parameters "
        "from the tool's inputSchema. For example, if a tool requires "
        '\'messageId\', you must pass {"arguments": {"messageId": "..."}}. '
        "Calling with empty arguments will fail."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Exact tool name from search_tools results",
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Tool arguments. MUST include all required parameters "
                    "from the tool's inputSchema returned by search_tools. "
                    "Use exact property names as shown in the schema."
                ),
            },
        },
        "required": ["tool_name", "arguments"],
    },
}

# Per-session discovered tools: session_id -> {tool_name: inputSchema}
_discovered_tools: dict[str, dict[str, dict]] = {}


async def _handle_search_tools(
    arguments: dict,
    agent_name: str | None,
    session_id: str,
    mcp_permissions: list[str] | None = None,
    unified_id: str | None = None,
) -> list[dict]:
    """Handle search_tools: find MCP tools by keyword matching."""
    import json

    query = arguments.get("query", "").strip().lower()
    category_filter = arguments.get("category", "").strip().lower()
    limit = min(arguments.get("limit", 10), 50)

    if not query:
        return [{"type": "text", "text": "Error: query is required."}]

    if not _client_manager:
        return [{"type": "text", "text": "Error: MCP client manager not initialized."}]

    # Get all available tools
    try:
        all_tools = await _client_manager.list_all_tools(
            unified_id=unified_id, agent_name=agent_name
        )
    except Exception as e:
        logger.error("search_tools: list_all_tools failed: %s", e)
        return [{"type": "text", "text": f"Error fetching tools: {e}"}]

    logger.info(
        "search_tools: query=%r, total_tools=%d, mcp_perms=%s, unified_id=%s",
        query,
        len(all_tools),
        mcp_permissions,
        unified_id,
    )
    if all_tools:
        sample_names = [t.get("name", "?") for t in all_tools[:5]]
        logger.info("search_tools: sample tool names: %s", sample_names)

    # Filter by MCP permissions
    if mcp_permissions:
        pre = len(all_tools)
        all_tools = _filter_by_permissions(all_tools, mcp_permissions)
        logger.info(
            "search_tools: permission filter %d→%d",
            pre,
            len(all_tools),
        )

    # Filter by per-user MCP permissions
    if unified_id:
        all_tools = _filter_by_user_mcp_permissions(all_tools, unified_id)

    # Filter by user preferences
    if unified_id:
        all_tools = _filter_by_user_prefs(all_tools, unified_id)

    # Log post-filter breakdown
    post_by_server = {}
    for t in all_tools:
        prefix = t["name"].split("__", 1)[0] if "__" in t["name"] else "builtin"
        post_by_server[prefix] = post_by_server.get(prefix, 0) + 1
    logger.info(
        "search_tools: post-filter %d tools: %s",
        len(all_tools),
        ", ".join(f"{k}:{v}" for k, v in sorted(post_by_server.items())),
    )

    # Score and rank tools
    keywords = query.split()
    scored: list[tuple[float, dict, str]] = []

    for tool in all_tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        name_lower = name.lower()
        desc_lower = desc.lower()

        # Determine category from server prefix
        server_prefix = name.split("__", 1)[0] if "__" in name else ""
        tool_category = _get_server_category(server_prefix)

        # Category filter
        if category_filter and tool_category.lower() != category_filter:
            continue

        # Keyword scoring: match in name = 3 points, match in desc = 1 point
        score = 0.0
        for kw in keywords:
            if kw in name_lower:
                score += 3.0  # Name match: 1 base + 2 bonus
            if kw in desc_lower:
                score += 1.0

        if score > 0:
            scored.append((score, tool, tool_category))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    # Update discovered tools for this session (store inputSchema for arg normalization)
    if session_id not in _discovered_tools:
        _discovered_tools[session_id] = {}
    for _, tool, _ in top:
        _discovered_tools[session_id][tool["name"]] = tool.get("inputSchema", {})

    # Build response
    if not top:
        return [
            {
                "type": "text",
                "text": f"No tools found matching '{query}'"
                + (f" in category '{category_filter}'" if category_filter else "")
                + ".",
            }
        ]

    results = []
    for score, tool, cat in top:
        desc = tool.get("description", "")
        if len(desc) > 200:
            desc = desc[:200] + "..."
        entry = {
            "name": tool["name"],
            "description": desc,
            "category": cat,
        }
        # Include inputSchema so the model knows what arguments to pass
        if "inputSchema" in tool:
            entry["inputSchema"] = tool["inputSchema"]
        results.append(entry)

    return [{"type": "text", "text": json.dumps(results, indent=2)}]


def _normalize_tool_args(args: dict, schema: dict) -> dict:
    """Normalize argument keys to match the tool's inputSchema.

    LLMs may convert camelCase property names to snake_case (e.g. messageId
    → message_id). This maps them back to the original schema property names
    so the MCP server receives the keys it expects.
    """
    properties = schema.get("properties", {})
    if not properties:
        return args

    # Build reverse mapping: snake_case → original camelCase name
    key_map: dict[str, str] = {}
    for prop_name in properties:
        snake = _camel_to_snake(prop_name)
        if snake != prop_name:
            key_map[snake] = prop_name

    if not key_map:
        return args

    normalized = {}
    for key, value in args.items():
        normalized[key_map.get(key, key)] = value
    return normalized


def _camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    import re

    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


async def _handle_execute_discovered(
    arguments: dict,
    agent_name: str | None,
    session_id: str,
    oauth2_user: str | None,
    mcp_permissions: list[str] | None = None,
) -> list[dict]:
    """Handle execute_discovered_tool: proxy to a previously discovered tool."""
    tool_name = arguments.get("tool_name", "").strip()
    tool_args = arguments.get("arguments", {})

    logger.debug(
        "execute_discovered_tool: tool=%s, raw_args_keys=%s, raw_args=%s",
        tool_name,
        list(tool_args.keys()) if tool_args else [],
        {k: repr(v)[:80] for k, v in tool_args.items()} if tool_args else {},
    )

    if not tool_name:
        return [{"type": "text", "text": "Error: tool_name is required."}]

    # Verify the tool was discovered in this session
    session_discovered = _discovered_tools.get(session_id, {})
    if tool_name not in session_discovered:
        return [
            {
                "type": "text",
                "text": (
                    f"Error: tool '{tool_name}' was not found via search_tools "
                    "in this session. Call search_tools first to discover it."
                ),
            }
        ]

    # Validate required arguments before dispatching to prevent
    # backend server crashes and circuit breaker cascades
    tool_schema = session_discovered.get(tool_name)
    if tool_schema:
        required = tool_schema.get("required", [])
        if required and not tool_args:
            props = tool_schema.get("properties", {})
            param_hints = ", ".join(
                f"{p} ({props[p].get('description', '')})" if p in props else p
                for p in required
            )
            return [
                {
                    "type": "text",
                    "text": (
                        f"Error: '{tool_name}' requires arguments: {required}. "
                        f"You MUST include them in the 'arguments' object. "
                        f"Parameters: {param_hints}"
                    ),
                }
            ]
        # Normalize argument keys: LLMs may convert camelCase to snake_case
        if tool_args:
            tool_args = _normalize_tool_args(tool_args, tool_schema)

    # Delegate to the standard dispatch (permissions already checked during search)
    t0 = time.monotonic()
    try:
        content = await _dispatch_tool_call(
            tool_name,
            tool_args,
            agent_name,
            oauth2_user,
            mcp_permissions=mcp_permissions,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _record_tool_usage(agent_name, tool_name, True, duration_ms)
        return content
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _record_tool_usage(agent_name, tool_name, False, duration_ms)
        logger.error("execute_discovered_tool error for %s: %s", tool_name, e)
        return [{"type": "text", "text": f"Error executing {tool_name}: {e}"}]


async def cleanup_tool_usage(retention_days: int = 90) -> int:
    """Delete tool_usage rows older than retention_days. Returns rows deleted."""
    try:
        from core.registry import get_database

        db = get_database()
        result = await db.execute(
            "DELETE FROM public.tool_usage WHERE called_at < NOW() - INTERVAL '%s days'",
            (retention_days,),
        )
        # psycopg returns the statusmessage; extract count if available
        deleted = getattr(result, "rowcount", 0) if result else 0
        if deleted:
            logger.info(
                "tool_usage cleanup: deleted %d rows older than %d days",
                deleted,
                retention_days,
            )
        return deleted
    except Exception as exc:
        logger.debug("tool_usage cleanup failed: %s", exc)
        return 0


async def _record_tool_usage(
    agent_name: str | None,
    tool_name: str,
    success: bool = True,
    duration_ms: int | None = None,
) -> None:
    """Record a tool call in the tool_usage table (fire-and-forget)."""
    if not agent_name:
        return
    try:
        from core.registry import get_database

        db = get_database()
        await db.execute(
            "INSERT INTO public.tool_usage (agent_name, tool_name, success, duration_ms) "
            "VALUES (%s, %s, %s, %s)",
            (agent_name, tool_name, success, duration_ms),
        )
    except Exception as exc:
        logger.debug("tool_usage recording failed: %s", exc)


async def _dispatch_tool_call(
    tool_name: str,
    arguments: dict,
    agent_name: str | None,
    oauth2_user: str | None,
    mcp_permissions: list[str] | None = None,
) -> list[dict]:
    """Dispatch a tool call to the appropriate handler. Returns MCP content list."""
    # Built-in virtual tools — always allowed, skip permission check
    if tool_name == "gridbear_help":
        tools = (
            await _client_manager.list_all_tools(
                unified_id=oauth2_user, agent_name=agent_name
            )
            if _client_manager
            else []
        )
        return _build_help_response(tools, arguments.get("category"))

    if tool_name == "send_file_to_chat":
        return await _handle_send_file(arguments, agent_name)

    if tool_name == "ask_agent":
        return await _handle_ask_agent(
            arguments,
            agent_name,
            oauth2_user,
            mcp_permissions,
        )

    if tool_name.startswith("chat_history__"):
        return await _handle_chat_history_call(tool_name, arguments)

    if tool_name.startswith("conversation_docs__"):
        return await _handle_conversation_docs_call(tool_name, arguments)

    if tool_name.startswith("credential_vault__"):
        return await _handle_vault_call(tool_name, arguments)

    # Build set of virtual tool prefixes (local plugins, no MCP permission needed)
    _virtual_prefixes = {"credential_vault__"}
    for vtp in _local_tool_providers:
        _virtual_prefixes.add(vtp.get_server_name().replace("-", "_") + "__")

    # Permission check for MCP tools (skip virtual tools — they are local plugins)
    is_virtual = any(tool_name.startswith(p) for p in _virtual_prefixes)
    if mcp_permissions is not None and "__" in tool_name and not is_virtual:
        from core.permissions.mcp_resolver import check_tool_permission

        if not check_tool_permission(
            tool_name,
            mcp_permissions,
            _client_manager._sanitized_to_original if _client_manager else {},
        ):
            logger.warning(
                "Permission denied: tool '%s' blocked for user=%s agent=%s",
                tool_name,
                oauth2_user,
                agent_name,
            )
            return [
                {
                    "type": "text",
                    "text": f"Permission denied: tool '{tool_name}' is not in the allowed MCP servers.",
                }
            ]

    # Plugin virtual tools
    for provider in _local_tool_providers:
        prefix = provider.get_server_name().replace("-", "_") + "__"
        if tool_name.startswith(prefix):
            return await provider.handle_tool_call(
                tool_name,
                arguments,
                agent_name=agent_name,
                oauth2_user=oauth2_user,
            )

    # Real MCP tools via client manager
    if not _client_manager:
        return [{"type": "text", "text": "Error: MCP client manager not initialized"}]

    result = await _client_manager.call_tool(
        tool_name,
        arguments,
        unified_id=oauth2_user,
        agent_name=agent_name,
    )
    return result


async def _send_task_notification(task) -> None:
    """Send a short notification + re-engage the agent after async task completion."""
    import httpx

    if not task.notify_chat_id or not task.notify_platform:
        return

    # --- Duration string ---
    duration = ""
    if task.completed_at and task.created_at:
        secs = task.completed_at - task.created_at
        if secs >= 60:
            duration = f" ({int(secs // 60)}m {int(secs % 60)}s)"
        else:
            duration = f" ({int(secs)}s)"

    # --- Clean tool name for display ---
    tool_short = task.tool_name.replace("__", " ")

    if task.status == "completed":
        text = f"✅ {tool_short} completed{duration}"
    else:
        text = f"❌ {tool_short} failed{duration}"

    internal_url = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
    internal_secret = os.getenv("INTERNAL_API_SECRET", "")
    headers = {"Authorization": f"Bearer {internal_secret}"}

    try:
        async with httpx.AsyncClient(timeout=15) as http_client:
            # Step 1: Send short notification to the user
            resp = await http_client.post(
                f"{internal_url}/api/send-message",
                json={
                    "agent_name": task.agent_name,
                    "platform": task.notify_platform,
                    "chat_id": task.notify_chat_id,
                    "text": text,
                },
                headers=headers,
            )
            if resp.status_code == 200:
                result = resp.json()
                if not result.get("ok"):
                    logger.warning(
                        "Async task notification delivery failed: %s",
                        result.get("error"),
                    )
            else:
                logger.warning("Async task notification HTTP %d", resp.status_code)

            # Step 2: Re-engage the agent via the full pipeline
            resp2 = await http_client.post(
                f"{internal_url}/api/process-task-result",
                json={
                    "agent_name": task.agent_name,
                    "platform": task.notify_platform,
                    "chat_id": task.notify_chat_id,
                    "tool_name": task.tool_name,
                    "task_id": task.id,
                    "status": task.status,
                    "duration": duration,
                },
                headers=headers,
            )
            if resp2.status_code == 200:
                result2 = resp2.json()
                if not result2.get("ok"):
                    logger.warning(
                        "Async task re-engage failed: %s", result2.get("error")
                    )
            else:
                logger.warning("Async task re-engage HTTP %d", resp2.status_code)

    except Exception as e:
        logger.error("Async task notification error: %s", e)


async def _handle_async_run_tool(
    arguments: dict,
    agent_name: str | None,
    oauth2_user: str | None,
    mcp_perms: list | None,
) -> list[dict]:
    """Handle async_run_tool: submit a tool call as a background task."""
    import json

    if not _task_manager:
        return [{"type": "text", "text": "Error: async task manager not initialized"}]

    if not agent_name:
        return [
            {
                "type": "text",
                "text": "Error: could not resolve agent from OAuth2 token.",
            }
        ]

    tool_name = arguments.get("tool_name", "")
    tool_args = arguments.get("arguments", {})
    notify_chat_id = arguments.get("notify_chat_id", "")
    notify_platform = arguments.get("notify_platform", "")

    if not tool_name:
        return [{"type": "text", "text": "Error: tool_name is required."}]

    # Don't allow wrapping async tools themselves (prevent recursion)
    if tool_name in ("async_run_tool", "async_task_status", "async_list_tasks"):
        return [
            {
                "type": "text",
                "text": "Error: cannot run async tools inside async_run_tool.",
            }
        ]

    # Verify the wrapped tool is permitted for this agent
    if mcp_perms:
        # Check virtual tools that don't need server permission
        virtual_tools = {"gridbear_help", "send_file_to_chat"}
        virtual_prefixes = ["credential_vault__"]
        # Add dynamic virtual tool provider prefixes
        for vtp in _local_tool_providers:
            virtual_prefixes.append(vtp.get_server_name().replace("-", "_") + "__")

        if tool_name not in virtual_tools and not any(
            tool_name.startswith(p) for p in virtual_prefixes
        ):
            # It's a real MCP tool — check if any server permission matches
            # We need to extract the server name from the tool
            # The permission check will be done by call_tool anyway,
            # but let's do a basic check here
            pass

    # Create the coroutine for background execution
    coro = _dispatch_tool_call(
        tool_name,
        tool_args,
        agent_name,
        oauth2_user,
        mcp_permissions=mcp_perms,
    )

    task_id = await _task_manager.submit(
        tool_name=tool_name,
        coro=coro,
        agent_name=agent_name,
        notify_chat_id=notify_chat_id,
        notify_platform=notify_platform,
    )

    result = {
        "task_id": task_id,
        "status": "running",
        "tool": tool_name,
        "message": f"Task submitted. You will be notified on {notify_platform} when it completes.",
    }
    return [{"type": "text", "text": json.dumps(result, indent=2)}]


async def _handle_async_task_status(
    arguments: dict,
    agent_name: str | None,
) -> list[dict]:
    """Handle async_task_status: check status of a background task."""
    import json

    if not _task_manager:
        return [{"type": "text", "text": "Error: async task manager not initialized"}]

    if not agent_name:
        return [{"type": "text", "text": "Error: could not resolve agent."}]

    task_id = arguments.get("task_id", "")
    if not task_id:
        return [{"type": "text", "text": "Error: task_id is required."}]

    status = _task_manager.get_status(task_id, agent_name)
    if not status:
        return [{"type": "text", "text": f"Error: task '{task_id}' not found."}]

    return [{"type": "text", "text": json.dumps(status, indent=2)}]


async def _handle_async_list_tasks(
    arguments: dict,
    agent_name: str | None,
) -> list[dict]:
    """Handle async_list_tasks: list background tasks for the agent."""
    import json

    if not _task_manager:
        return [{"type": "text", "text": "Error: async task manager not initialized"}]

    if not agent_name:
        return [{"type": "text", "text": "Error: could not resolve agent."}]

    status_filter = arguments.get("status")
    tasks = _task_manager.list_tasks(agent_name, status_filter)

    if not tasks:
        return [{"type": "text", "text": "No async tasks found."}]

    return [{"type": "text", "text": json.dumps(tasks, indent=2)}]


def _build_help_response(tools: list[dict], category: str | None = None) -> list[dict]:
    """Build gridbear_help tool response."""
    from core.mcp_gateway.client_manager import NS_SEP

    # Group tools by namespace
    categories: dict[str, list[dict]] = {}
    for tool in tools:
        name = tool.get("name", "")
        if NS_SEP in name:
            ns = name.split(NS_SEP, 1)[0]
        else:
            ns = "other"
        categories.setdefault(ns, []).append(tool)

    if category:
        cat_tools = categories.get(category, [])
        if not cat_tools:
            return [
                {
                    "type": "text",
                    "text": f"Category '{category}' not found. Use gridbear_help without arguments to see available categories.",
                }
            ]
        lines = [f"## {category} ({len(cat_tools)} tools)\n"]
        for t in cat_tools:
            desc = t.get("description", "")[:80]
            lines.append(f"- **{t['name']}**: {desc}")
        return [{"type": "text", "text": "\n".join(lines)}]
    else:
        lines = ["## GridBear MCP Gateway - Available Categories\n"]
        for ns in sorted(categories):
            count = len(categories[ns])
            # Use first tool's description to infer category purpose
            lines.append(f"- **{ns}**: {count} tools")
        lines.append(f"\nTotal: {sum(len(v) for v in categories.values())} tools")
        lines.append(
            "\nCall `gridbear_help` with `category` argument for tool details."
        )
        return [{"type": "text", "text": "\n".join(lines)}]


def _get_active_platforms() -> list[str]:
    """Get active platform/channel names from plugin manager."""
    from core.registry import get_plugin_manager

    pm = get_plugin_manager()
    if not pm:
        return []
    # _manifests contains all loaded plugins; filter by type=channel
    platforms = ["webchat"]
    for name, manifest in pm._manifests.items():
        if manifest.get("type") == "channel":
            platforms.append(name)
    return sorted(platforms) if platforms else []


def _inject_platform_enum(tool: dict, field_name: str, platforms: list[str]) -> dict:
    """Return a copy of a tool definition with a platform enum injected."""
    import copy

    if not platforms:
        return tool
    tool = copy.deepcopy(tool)
    props = tool.get("inputSchema", {}).get("properties", {})
    if field_name in props:
        props[field_name]["enum"] = platforms
    return tool


async def _handle_message(msg: dict, session_id: str, request: Request) -> dict | None:
    """Handle a single JSON-RPC message. Returns response or None for notifications."""
    global _last_refresh

    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    # Notifications (no id) don't get responses
    if msg_id is None:
        if method == "notifications/initialized":
            logger.info(f"MCP Gateway: client initialized (session={session_id[:8]})")
        else:
            logger.debug(
                "MCP Gateway: notification %s (session=%s)", method, session_id[:8]
            )
        return None

    if method == "initialize":
        # Negotiate protocol version: accept client's version if we support it,
        # otherwise fall back to our latest supported version.
        client_version = params.get("protocolVersion", "2025-03-26")
        supported_versions = {"2024-11-05", "2025-03-26", "2025-11-25"}
        negotiated = (
            client_version if client_version in supported_versions else "2025-03-26"
        )
        client_info = params.get("clientInfo", {})
        _sessions[session_id] = {
            "client_info": client_info,
            "protocol_version": negotiated,
        }
        logger.info(
            "MCP Gateway: initialize session=%s client=%s v=%s → negotiated=%s",
            session_id[:8],
            client_info.get("name", "?"),
            client_version,
            negotiated,
        )
        return _jsonrpc_response(
            msg_id,
            {
                "protocolVersion": negotiated,
                "capabilities": MCP_CAPABILITIES,
                "serverInfo": MCP_SERVER_INFO,
            },
        )

    if method == "ping":
        return _jsonrpc_response(msg_id, {})

    if method == "tools/list":
        if not _client_manager:
            logger.warning("MCP Gateway: client manager not initialized")
            return _jsonrpc_response(msg_id, {"tools": []})

        client = getattr(request.state, "oauth2_client", None)
        agent_name = client.agent_name if client else None

        # Refresh providers periodically
        now = time.time()
        if now - _last_refresh > _REFRESH_INTERVAL:
            _last_refresh = now
            try:
                await _client_manager.refresh_providers()
                _build_server_categories()
            except Exception as e:
                logger.warning(f"MCP Gateway: provider refresh failed: {e}")

        # Aggregate tools from all servers (pass user identity for user-aware servers)
        # Priority: explicit JSON-RPC param > side-channel context > token identity
        oauth2_user = getattr(request.state, "oauth2_user", None)
        effective_user = (
            params.get("user_identity")
            or _agent_user_context.get(agent_name)
            or oauth2_user
        )

        # Check tool_loading mode: "search" = skip MCP tools, return only built-in
        # Fallback to agent YAML config when runner doesn't send these params
        # (e.g. Claude CLI process pool)
        tool_loading = params.get("tool_loading")
        tool_budget_param = params.get("tool_budget")
        if agent_name and (not tool_loading or not tool_budget_param):
            agent_cfg = _load_agent_config(agent_name)
            if agent_cfg:
                if not tool_loading:
                    tool_loading = agent_cfg.get("tool_loading", "full")
                if not tool_budget_param:
                    tool_budget_param = agent_cfg.get("max_tools")
        tool_loading = tool_loading or "full"
        is_search_mode = tool_loading == "search"

        # Build cache key from parameters that affect the tool list
        mcp_perms = getattr(request.state, "oauth2_mcp_permissions", None)
        cache_key = f"{agent_name}:{effective_user}:{tool_loading}:{tool_budget_param}"
        if mcp_perms:
            cache_key += ":" + ",".join(sorted(mcp_perms))

        # Check tools cache
        now = time.time()
        cached = _tools_cache.get(cache_key)
        if cached and (now - cached[0]) < _TOOLS_CACHE_TTL:
            tools = list(cached[1])  # shallow copy
            logger.debug(
                "tools/list cache hit for key=%s (%d tools)", cache_key[:40], len(tools)
            )
        else:
            # Cache miss — build the full tool list
            if is_search_mode:
                # Search mode: no MCP tools loaded
                tools = []
            else:
                try:
                    tools = await _client_manager.list_all_tools(
                        unified_id=effective_user, agent_name=agent_name
                    )
                except Exception as e:
                    logger.error(f"MCP Gateway: tools/list failed: {e}")
                    tools = []

                # Filter by OAuth2 MCP permissions
                if mcp_perms:
                    pre_filter = len(tools)
                    tools = _filter_by_permissions(tools, mcp_perms)
                    logger.info(
                        "MCP Gateway: tools/list filtered %d→%d (perms=%s)",
                        pre_filter,
                        len(tools),
                        mcp_perms,
                    )

                # Filter by user tool preferences
                if effective_user:
                    tools = _filter_by_user_prefs(tools, effective_user)

                # Filter by per-agent tool preferences (admin-managed)
                if agent_name:
                    tools = _filter_by_agent_prefs(tools, agent_name)

                # Apply tool budget (MCP tools only — built-in added after)
                tool_budget = tool_budget_param
                if tool_budget and isinstance(tool_budget, int) and tool_budget > 0:
                    pre_budget = len(tools)
                    if pre_budget > tool_budget:
                        tools = _apply_tool_budget(tools, tool_budget, agent_name=None)
                        logger.info(
                            "Tool budget applied: %d→%d MCP tools",
                            pre_budget,
                            tool_budget,
                        )

            # Add gridbear_help meta-tool
            tools.append(_HELP_TOOL)

            # Add credential-vault tools if agent has permission
            if mcp_perms:
                from core.permissions.mcp_resolver import matches_permission

                if matches_permission(_VAULT_SERVER_NAME, mcp_perms):
                    tools.extend(_VAULT_TOOLS)

            # Add virtual tool provider tools (filtered by agent config)
            # Include if: in plugins.enabled OR in mcp_permissions
            agent_allowed_vtp = None
            if agent_name:
                acfg = _load_agent_config(agent_name)
                if acfg:
                    allowed = set()
                    plugins_cfg = acfg.get("plugins", {})
                    if isinstance(plugins_cfg, dict) and "enabled" in plugins_cfg:
                        allowed.update(plugins_cfg["enabled"])
                    agent_perms = acfg.get("mcp_permissions", [])
                    if isinstance(agent_perms, list):
                        allowed.update(agent_perms)
                    agent_allowed_vtp = allowed
            for provider in _local_tool_providers:
                if agent_allowed_vtp is not None:
                    server_name = provider.get_server_name()
                    if server_name not in agent_allowed_vtp:
                        continue
                tools.extend(provider.get_tools())

            # Discover active platform names for enum injection
            platform_names = _get_active_platforms()

            # Add send_file_to_chat tool (with dynamic platform enum)
            send_file_tool = _inject_platform_enum(
                _SEND_FILE_TOOL, "platform", platform_names
            )
            tools.append(send_file_tool)

            # Add ask_agent tool (always available, inter-agent communication)
            tools.append(_ASK_AGENT_TOOL)

            # Add async task tools (with dynamic platform enum)
            async_tools = []
            for tool in _ASYNC_TOOLS:
                if tool["name"] == "async_run_tool":
                    tool = _inject_platform_enum(
                        tool, "notify_platform", platform_names
                    )
                async_tools.append(tool)
            tools.extend(async_tools)

            # Add chat history tools (always available)
            tools.extend(_CHAT_HISTORY_TOOLS)

            # Add conversation document tools (always available)
            tools.extend(_CONVERSATION_DOCS_TOOLS)

            # Add search_tools + execute_discovered_tool (always available)
            tools.append(_SEARCH_TOOLS_TOOL)
            tools.append(_EXECUTE_DISCOVERED_TOOL)

            # Store in cache (before user-level filtering which is per-user)
            _tools_cache[cache_key] = (now, list(tools))

        # Filter by per-user MCP permissions (admin-managed in /admin/permissions).
        # Runs after ALL tools are assembled (MCP + virtual + built-in) so that
        # virtual tool providers are also subject to user restrictions.
        if effective_user:
            tools = _filter_by_user_mcp_permissions(tools, effective_user)

        # Structured logging with per-server breakdown
        by_server = {}
        for t in tools:
            prefix = t["name"].split("__", 1)[0] if "__" in t["name"] else "builtin"
            by_server[prefix] = by_server.get(prefix, 0) + 1
        breakdown = ", ".join(f"{k}:{v}" for k, v in sorted(by_server.items()))
        logger.info(
            "tools/list for agent=%s user=%s: %d tools (%s)",
            agent_name,
            effective_user,
            len(tools),
            breakdown,
        )

        return _jsonrpc_response(msg_id, {"tools": tools})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        client = getattr(request.state, "oauth2_client", None)
        agent_name = client.agent_name if client else None
        oauth2_user = getattr(request.state, "oauth2_user", None)
        # Priority: explicit JSON-RPC param > side-channel context > token identity
        effective_user = (
            params.get("user_identity")
            or _agent_user_context.get(agent_name)
            or oauth2_user
        )
        mcp_perms = getattr(request.state, "oauth2_mcp_permissions", None)

        # Enforce per-user MCP permissions on tool calls (not just tools/list).
        # Non-namespaced tools (built-in) are always allowed.
        if (
            effective_user
            and "__" in tool_name
            and not effective_user.startswith("inter_agent:")
        ):
            from config.settings import get_user_mcp_permissions
            from core.permissions.mcp_resolver import matches_permission

            user_perms = get_user_mcp_permissions(effective_user)
            if user_perms is not None:
                ns_sep = "__"
                sanitized = tool_name[: tool_name.index(ns_sep)]
                reverse_map = (
                    _client_manager._sanitized_to_original if _client_manager else {}
                )
                original = reverse_map.get(sanitized, sanitized)
                if not matches_permission(original, user_perms):
                    logger.warning(
                        "MCP Gateway: user %s denied tool %s (user_perms=%s)",
                        effective_user,
                        tool_name,
                        user_perms,
                    )
                    return _jsonrpc_response(
                        msg_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Error: you don't have permission to use {tool_name}.",
                                }
                            ],
                            "isError": True,
                        },
                    )
            elif user_perms is None:
                # No permissions configured = no MCP tools allowed
                logger.warning(
                    "MCP Gateway: user %s has no MCP permissions, denied tool %s",
                    effective_user,
                    tool_name,
                )
                return _jsonrpc_response(
                    msg_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Error: you don't have permission to use {tool_name}.",
                            }
                        ],
                        "isError": True,
                    },
                )

        # Handle async task tools
        if tool_name == "async_run_tool":
            try:
                content = await _handle_async_run_tool(
                    arguments,
                    agent_name,
                    effective_user,
                    mcp_perms,
                )
            except Exception as e:
                logger.error("async_run_tool error: %s", e)
                content = [{"type": "text", "text": f"Error: {e}"}]
            return _jsonrpc_response(msg_id, {"content": content})

        if tool_name == "async_task_status":
            try:
                content = await _handle_async_task_status(arguments, agent_name)
            except Exception as e:
                logger.error("async_task_status error: %s", e)
                content = [{"type": "text", "text": f"Error: {e}"}]
            return _jsonrpc_response(msg_id, {"content": content})

        if tool_name == "async_list_tasks":
            try:
                content = await _handle_async_list_tasks(arguments, agent_name)
            except Exception as e:
                logger.error("async_list_tasks error: %s", e)
                content = [{"type": "text", "text": f"Error: {e}"}]
            return _jsonrpc_response(msg_id, {"content": content})

        # Handle search_tools
        if tool_name == "search_tools":
            try:
                content = await _handle_search_tools(
                    arguments,
                    agent_name,
                    session_id,
                    mcp_permissions=mcp_perms,
                    unified_id=effective_user,
                )
            except Exception as e:
                logger.error("search_tools error: %s", e)
                content = [{"type": "text", "text": f"Error: {e}"}]
            return _jsonrpc_response(msg_id, {"content": content})

        # Handle execute_discovered_tool
        if tool_name == "execute_discovered_tool":
            try:
                content = await _handle_execute_discovered(
                    arguments,
                    agent_name,
                    session_id,
                    effective_user,
                    mcp_permissions=mcp_perms,
                )
            except Exception as e:
                logger.error("execute_discovered_tool error: %s", e)
                content = [{"type": "text", "text": f"Error: {e}"}]
            return _jsonrpc_response(msg_id, {"content": content})

        # Dispatch all other tool calls through centralized handler
        t0 = time.monotonic()
        try:
            content = await _dispatch_tool_call(
                tool_name,
                arguments,
                agent_name,
                effective_user,
                mcp_permissions=mcp_perms,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
            await _record_tool_usage(agent_name, tool_name, True, duration_ms)
            return _jsonrpc_response(msg_id, {"content": content})
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            await _record_tool_usage(agent_name, tool_name, False, duration_ms)
            if type(e).__name__ == "ToolNotFoundError":
                # Preserve the original message for better diagnostics
                return _jsonrpc_error(msg_id, -32601, str(e))
            logger.error(
                "MCP Gateway: tool call error: type=%s repr=%r",
                type(e).__name__,
                e,
            )
            error_msg = str(e) or f"{type(e).__name__}: tool call failed"
            # Return as tool result with isError so the model can reason
            # about the failure instead of triggering protocol-level retries
            return _jsonrpc_response(
                msg_id,
                {
                    "content": [{"type": "text", "text": f"Error: {error_msg}"}],
                    "isError": True,
                },
            )

    if method == "resources/list":
        return _jsonrpc_response(msg_id, {"resources": []})

    if method == "prompts/list":
        return _jsonrpc_response(msg_id, {"prompts": []})

    return _jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_post(request: Request):
    """Handle client JSON-RPC messages."""
    error = _check_bearer(request)
    if error:
        return error

    # Rate limit per token
    token_id = str(
        getattr(request.state, "oauth2_token", {}).id
        if hasattr(request.state, "oauth2_token")
        else "unknown"
    )
    retry_after = _rate_limiter.check(
        f"mcp:{token_id}", _RATE_LIMIT_MAX_REQUESTS, _RATE_LIMIT_WINDOW
    )
    if retry_after is not None:
        resp = api_error(429, "rate_limit_exceeded", "rate_limit")
        resp.headers["Retry-After"] = str(int(retry_after) + 1)
        return resp

    # Get or create session.
    # For native MCP clients (Claude CLI) the header carries a stable session ID.
    # For custom tool adapters (Gemini/Claude API) there's no header, so we derive
    # a stable session key from the OAuth2 client's agent_name — this ensures
    # search_tools and execute_discovered_tool share the same _discovered_tools set.
    session_id = request.headers.get("mcp-session-id", "")
    if not session_id:
        client = getattr(request.state, "oauth2_client", None)
        if client and client.agent_name:
            session_id = f"agent:{client.agent_name}"
        else:
            session_id = str(uuid.uuid4())

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_jsonrpc_error(None, -32700, "Parse error"),
            status_code=400,
        )

    # Handle batch or single message
    if isinstance(body, list):
        responses = []
        for msg in body:
            resp = await _handle_message(msg, session_id, request)
            if resp is not None:
                responses.append(resp)
        result = responses if len(responses) != 1 else responses[0]
    else:
        result = await _handle_message(body, session_id, request)

    if result is None:
        # Notification - accepted with no content
        return Response(
            status_code=202,
            headers={"Mcp-Session-Id": session_id},
        )

    return JSONResponse(
        content=result,
        headers={"Mcp-Session-Id": session_id},
    )


@router.get("/mcp")
async def mcp_sse(request: Request):
    """SSE stream for server-initiated notifications."""
    error = _check_bearer(request)
    if error:
        return error

    # Rate limit: max concurrent SSE connections per client
    client = getattr(request.state, "oauth2_client", None)
    client_id = client.client_id if client else "unknown"
    active = _rate_limiter.count_active(f"sse:{client_id}:")
    if active >= _MAX_SSE_CONNECTIONS:
        resp = api_error(429, "too_many_connections", "rate_limit")
        resp.headers["Retry-After"] = str(_SSE_KEEPALIVE_INTERVAL)
        return resp

    conn_key = f"sse:{client_id}:{uuid.uuid4().hex[:8]}"
    session_id = request.headers.get("mcp-session-id", str(uuid.uuid4()))

    async def event_stream():
        # Track this SSE connection
        _rate_limiter.check(conn_key, 9999, 3600)  # register in window
        # Send initial keepalive
        yield ": keepalive\n\n"
        import asyncio

        try:
            while True:
                await asyncio.sleep(_SSE_KEEPALIVE_INTERVAL)
                # Refresh timestamp to keep connection tracked
                _rate_limiter.check(conn_key, 9999, 3600)
                yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            # Remove from tracking
            _rate_limiter._windows.pop(conn_key, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Mcp-Session-Id": session_id,
        },
    )


@router.delete("/mcp")
async def mcp_delete(request: Request):
    """Close MCP session."""
    error = _check_bearer(request)
    if error:
        return error

    session_id = request.headers.get("mcp-session-id", "")
    if session_id and session_id in _sessions:
        del _sessions[session_id]
        logger.info(f"MCP Gateway: session closed ({session_id[:8]})")
    # Clean up discovered tools for this session
    _discovered_tools.pop(session_id, None)
    return Response(status_code=204)


@router.post("/mcp/user-context")
async def mcp_set_user_context(request: Request):
    """Set current user identity for an agent (CLI runner side-channel).

    The CLI process is a black box — we can't inject user_identity into its
    JSON-RPC params.  Instead, the runner POSTs the identity here before each
    prompt, and _handle_search_tools / _handle_tool_call read it as a fallback.
    """
    error = _check_bearer(request)
    if error:
        return error

    client = getattr(request.state, "oauth2_client", None)
    agent_name = client.agent_name if client else None
    if not agent_name:
        return api_error(400, "no agent", "validation_error")

    try:
        body = await request.json()
    except Exception:
        return api_error(400, "invalid json", "validation_error")

    new_user = body.get("user_identity")
    old_user = _agent_user_context.get(agent_name)
    _agent_user_context[agent_name] = new_user
    if old_user != new_user:
        # Tools cache is keyed on (agent, effective_user, ...); an
        # identity swap makes prior entries stale, so drop them.
        _invalidate_tools_cache(agent_name)
    logger.debug(
        "user context set: agent=%s user=%s",
        agent_name,
        new_user,
    )
    return api_ok()


# ==================== INSPECTION ENDPOINTS ====================


@router.get("/mcp/servers")
async def mcp_list_servers(request: Request):
    """List all known MCP servers with runtime status."""
    error = _check_bearer(request)
    if error:
        return error

    if not _client_manager:
        return api_error(503, "MCP gateway not initialized", "not_ready")

    servers = []
    for name, info in _client_manager._known_servers.items():
        connected = name in _client_manager._connections
        cb = _client_manager._circuit_breakers.get(name)
        cb_state = cb.state if cb else "closed"
        servers.append(
            {
                "name": name,
                "transport": info.transport,
                "user_aware": info.user_aware,
                "category": info.category,
                "plugin": info.plugin_dir,
                "service_connection_id": info.service_connection_id,
                "connected": connected,
                "circuit_breaker": cb_state,
            }
        )

    return api_ok(servers, count=len(servers))


@router.get("/mcp/servers/{name:path}")
async def mcp_get_server(request: Request, name: str):
    """Get detail for a single MCP server."""
    error = _check_bearer(request)
    if error:
        return error

    if not _client_manager:
        return api_error(503, "MCP gateway not initialized", "not_ready")

    info = _client_manager._known_servers.get(name)
    if not info:
        return api_error(404, f"Server '{name}' not found", "not_found")

    connected = name in _client_manager._connections
    cb = _client_manager._circuit_breakers.get(name)
    cb_state = cb.state if cb else "closed"

    # Sanitize config — mask env/header values
    sanitized_config = {}
    for key, val in (info.config or {}).items():
        if key == "env":
            sanitized_config[key] = {k: "***" for k in (val or {})}
        elif key == "headers":
            sanitized_config[key] = {k: "***" for k in (val or {})}
        else:
            sanitized_config[key] = val

    # Get tool names from connection if available
    tool_list = []
    conn = _client_manager._connections.get(name)
    if conn and hasattr(conn, "tools"):
        for t in conn.tools or []:
            tool_list.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                }
            )

    detail = {
        "name": name,
        "transport": info.transport,
        "user_aware": info.user_aware,
        "category": info.category,
        "plugin": info.plugin_dir,
        "service_connection_id": info.service_connection_id,
        "connected": connected,
        "circuit_breaker": cb_state,
        "config": sanitized_config,
        "tools": tool_list,
        "tool_count": len(tool_list),
    }

    return api_ok(detail)


@router.get("/mcp/creds/{connection_id}")
async def mcp_check_creds(request: Request, connection_id: str):
    """Check credential status for a user + connection (no values exposed)."""
    error = _check_bearer(request)
    if error:
        return error

    user = request.query_params.get("user")
    if not user:
        return api_error(400, "Query parameter 'user' is required", "validation_error")

    agent = request.query_params.get("agent")

    from .client_manager import _get_user_credentials

    creds = _get_user_credentials(user, connection_id, agent_name=agent)

    result = {
        "connection_id": connection_id,
        "user": user,
        "status": "connected" if creds else "not_connected",
    }
    if creds:
        result["type"] = creds.get("_type", "unknown")
        result["is_global"] = creds.get("_global", False)
        result["is_agent"] = creds.get("_agent", False)

    return api_ok(result)
