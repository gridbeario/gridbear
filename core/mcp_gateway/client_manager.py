"""MCP Gateway - Client Manager.

Manages connections to MCP servers, aggregates tools, and routes tool calls.
Uses the MCP Python SDK for stdio and SSE transports.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field

from config.logging_config import logger
from core.mcp_gateway.provider_loader import ServerInfo, discover_mcp_servers

# Namespace separator for tool names
NS_SEP = "__"

# MCP tool name pattern: only alphanumeric, underscore, hyphen, max 64 chars
_MCP_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_name(name: str) -> str:
    """Sanitize a server name for use in MCP tool names.

    Replaces invalid characters (like @ and .) with underscores.
    MCP tool names must match: ^[a-zA-Z0-9_-]{1,64}$
    """
    return _MCP_NAME_RE.sub("_", name)


# camelCase → snake_case pattern
_CAMEL_RE1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_RE2 = re.compile(r"([a-z0-9])([A-Z])")


def _normalize_tool_args(args: dict, tool_name: str, tools: list[dict]) -> dict:
    """Normalize argument keys to match the tool's inputSchema.

    LLMs may convert camelCase property names to snake_case when generating
    tool arguments (e.g. messageId → message_id).  This maps them back to
    the original schema property names so the MCP server receives the keys
    it expects.  Works for any runner (Claude, Mistral, Gemini, etc.).
    """
    if not args:
        return args

    # Find matching tool schema
    schema = None
    for tool in tools:
        if tool.get("name") == tool_name:
            schema = tool.get("inputSchema", {})
            break

    if not schema:
        return args

    properties = schema.get("properties", {})
    if not properties:
        return args

    # Build reverse mapping: snake_case → original camelCase name
    key_map: dict[str, str] = {}
    for prop_name in properties:
        snake = _CAMEL_RE2.sub(r"\1_\2", _CAMEL_RE1.sub(r"\1_\2", prop_name)).lower()
        if snake != prop_name:
            key_map[snake] = prop_name

    if not key_map:
        return args

    # Only remap keys that need it
    normalized = {}
    changed = False
    for key, value in args.items():
        new_key = key_map.get(key, key)
        if new_key != key:
            changed = True
        normalized[new_key] = value

    if changed:
        logger.debug(
            "Normalized tool args for %s: %s",
            tool_name,
            {k: v for k, v in zip(args.keys(), normalized.keys()) if k != v},
        )

    return normalized if changed else args


# Idle timeout: disconnect servers not used for this many seconds
IDLE_TIMEOUT = int(os.getenv("MCP_IDLE_TIMEOUT", "300"))

# Cleanup check interval
CLEANUP_INTERVAL = int(os.getenv("MCP_CLEANUP_INTERVAL", "60"))

# Circuit breaker defaults — transport failures
CB_FAILURE_THRESHOLD = int(os.getenv("MCP_CB_FAILURE_THRESHOLD", "3"))
CB_FAILURE_WINDOW = float(os.getenv("MCP_CB_FAILURE_WINDOW", "120"))
CB_RECOVERY_TIMEOUT = float(os.getenv("MCP_CB_RECOVERY_TIMEOUT", "30"))

# Circuit breaker — soft failures (isError from tool calls)
CB_SOFT_FAILURE_THRESHOLD = int(os.getenv("MCP_CB_SOFT_FAILURE_THRESHOLD", "10"))
CB_SOFT_FAILURE_WINDOW = float(os.getenv("MCP_CB_SOFT_FAILURE_WINDOW", "300"))


class ToolNotFoundError(Exception):
    """Raised when a namespaced tool name cannot be resolved."""


class CircuitBreaker:
    """Dual-track circuit breaker for MCP server connections.

    Tracks two independent failure streams:
    - **Transport failures** (connection errors, crashes): low threshold (3/120s)
    - **Soft failures** (``isError=True`` from tool calls): high threshold (10/300s)

    Either track reaching its threshold opens the circuit.

    States:
    - closed: normal operation, errors tracked
    - open: too many errors, requests blocked
    - half_open: testing with a single request after cooldown
    """

    def __init__(
        self,
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        failure_window: float = CB_FAILURE_WINDOW,
        recovery_timeout: float = CB_RECOVERY_TIMEOUT,
        soft_failure_threshold: int = CB_SOFT_FAILURE_THRESHOLD,
        soft_failure_window: float = CB_SOFT_FAILURE_WINDOW,
    ):
        self.failure_threshold = failure_threshold
        self.failure_window = failure_window
        self.recovery_timeout = recovery_timeout
        self.soft_failure_threshold = soft_failure_threshold
        self.soft_failure_window = soft_failure_window
        self._failures: list[float] = []
        self._soft_failures: list[float] = []
        self._state = "closed"  # closed | open | half_open
        self._opened_at: float = 0
        self._half_open_in_flight: bool = False

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.time() - self._opened_at >= self.recovery_timeout:
                self._state = "half_open"
        return self._state

    def record_success(self) -> None:
        """Record a successful call.  Clears both failure tracks."""
        self._failures.clear()
        self._soft_failures.clear()
        self._state = "closed"
        self._half_open_in_flight = False

    def record_failure(self) -> None:
        """Record a transport failure.  Opens circuit if threshold reached."""
        now = time.time()
        self._half_open_in_flight = False
        self._failures.append(now)
        cutoff = now - self.failure_window
        self._failures = [t for t in self._failures if t >= cutoff]

        if len(self._failures) >= self.failure_threshold:
            self._state = "open"
            self._opened_at = now

    def record_soft_failure(self) -> None:
        """Record a soft failure (``isError=True``).

        Uses a higher threshold / wider window than transport failures to
        avoid false positives from LLM retry patterns with invalid input.
        """
        now = time.time()
        self._soft_failures.append(now)
        cutoff = now - self.soft_failure_window
        self._soft_failures = [t for t in self._soft_failures if t >= cutoff]

        if len(self._soft_failures) >= self.soft_failure_threshold:
            self._state = "open"
            self._opened_at = now

    def allow_request(self) -> bool:
        """Check if a request should be allowed.

        In half-open state, only one request is permitted (the probe).
        """
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._half_open_in_flight:
            self._half_open_in_flight = True
            return True
        return False  # open, or half_open with probe in flight

    def reset(self) -> None:
        """Reset to closed state."""
        self._failures.clear()
        self._soft_failures.clear()
        self._state = "closed"
        self._opened_at = 0
        self._half_open_in_flight = False


@dataclass
class MCPServerConnection:
    """Active connection to a single MCP server."""

    server_info: ServerInfo
    session: object = None  # mcp.ClientSession
    transport_ctx: object = None  # context manager
    read_stream: object = None
    write_stream: object = None
    tools: list[dict] = field(default_factory=list)
    last_used: float = field(default_factory=time.time)
    connected: bool = False
    failed: bool = False  # True after connection failure
    failed_at: float = 0  # Timestamp of failure (for retry after cooldown)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _wire_field(model, wire_name: str, default=None):
    """Read a field by the name it carries on the wire, not by its attribute.

    The SDK renames fields between majors while keeping the protocol alias
    stable — inputSchema became input_schema, isError became is_error — so
    reading by attribute silently breaks on an upgrade while reading by alias
    does not.
    """
    for attr, info in type(model).model_fields.items():
        if (info.alias or attr) == wire_name:
            return getattr(model, attr)
    return default


def _get_user_credentials(
    unified_id: str,
    connection_id: str,
    agent_name: str | None = None,
) -> dict | None:
    """Get credentials for a service connection.

    Lookup priority: per-user (including memory-group aliases) → per-agent → global.
    Returns dict with credentials or None if not connected.
    """
    try:
        from ui.secrets_manager import secrets_manager
    except ImportError:
        return None

    import json

    # Credentials are per-user (bound to a specific OAuth2 flow),
    # NOT shared across memory group aliases
    candidates = [unified_id]

    # Try per-user credentials for each candidate
    for uid in candidates:
        for suffix in ("token", "api_key", "credentials"):
            key = f"user:{uid}:svc:{connection_id}:{suffix}"
            value = secrets_manager.get_plain(key)
            if value:
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        parsed["_type"] = suffix
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                return {"_type": suffix, suffix: value}

    # Agent-level credentials (per-agent SA, tokens, etc.)
    if agent_name:
        for suffix in ("token", "api_key", "credentials"):
            key = f"agent:{agent_name}:svc:{connection_id}:{suffix}"
            value = secrets_manager.get_plain(key)
            if value:
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        parsed["_type"] = suffix
                        parsed["_agent"] = True
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
                return {"_type": suffix, suffix: value, "_agent": True}

    # Fallback: try global credentials (backward compatibility)
    for suffix in ("token", "api_key", "credentials"):
        key = f"svc:{connection_id}:{suffix}"
        value = secrets_manager.get_plain(key)
        if value:
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    parsed["_type"] = suffix
                    parsed["_global"] = True
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return {"_type": suffix, suffix: value, "_global": True}

    return None


def _mark_token_expired(unified_id: str, connection_id: str) -> None:
    """Mark a user's OAuth2 token as expired in the vault.

    Sets expires_at to 0 so _is_token_expired() picks it up and the
    /me/connections page shows the amber "Expired" badge.
    """
    import json

    try:
        from ui.secrets_manager import secrets_manager
    except ImportError:
        return

    key = f"user:{unified_id}:svc:{connection_id}:token"
    raw = secrets_manager.get_plain(key)
    if not raw:
        return

    try:
        token_data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    if not isinstance(token_data, dict):
        return

    token_data["expires_at"] = 0
    secrets_manager.set(key, json.dumps(token_data))
    logger.info(
        "MCP Gateway: marked token expired for user=%s connection=%s",
        unified_id,
        connection_id,
    )


class MCPClientManager:
    """Manages connections to all MCP servers and aggregates tools."""

    def __init__(self):
        self._known_servers: dict[str, ServerInfo] = {}
        self._connections: dict[str, MCPServerConnection] = {}
        self._user_connections: dict[str, MCPServerConnection] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._tool_cache: list[dict] | None = None
        self._tool_cache_time: float = 0
        # Mapping: sanitized_name -> original server_name (for reverse lookup)
        self._sanitized_to_original: dict[str, str] = {}

    def _get_circuit_breaker(self, server_name: str) -> CircuitBreaker:
        """Get or create a circuit breaker for the given server.

        Circuit breakers live on the manager (not on MCPServerConnection)
        so their state survives connection teardowns.
        """
        if server_name not in self._circuit_breakers:
            self._circuit_breakers[server_name] = CircuitBreaker()
        return self._circuit_breakers[server_name]

    async def start(self) -> None:
        """Start the client manager and background cleanup task."""
        await self.refresh_providers()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("MCP Gateway: client manager started")

    async def warm_up(self) -> None:
        """Pre-connect to all admin-configured servers to cache their tool lists.

        Connects non-user-aware servers and user-aware servers that have
        server configs (admin-configured accounts with baked-in credentials,
        e.g. Gmail).  Truly user-only servers (no server config) are skipped.

        For user-aware SSE/HTTP servers that require per-user credentials,
        performs a lightweight HTTP health check instead of a full MCP connect.

        Runs in background — failures are logged and ignored.
        """
        for name, info in self._known_servers.items():
            if info.user_aware and info.service_connection_id:
                # Requires per-user OAuth2 credentials — can't do full MCP connect.
                # Do a lightweight HTTP reachability check instead.
                await self._health_check_sse(name, info)
                continue
            if (
                info.user_aware
                and "command" not in info.config
                and "url" not in info.config
            ):
                # Purely user-portal server with no admin config — skip
                continue
            try:
                conn = await self._ensure_connected(name)
                if conn and conn.connected:
                    logger.debug("MCP warm-up: %s (%d tools)", name, len(conn.tools))
            except Exception as exc:
                logger.debug("MCP warm-up: %s failed: %s", name, exc)

    async def _health_check_sse(self, name: str, info: ServerInfo) -> None:
        """Lightweight HTTP health check for user-aware SSE/HTTP servers.

        Sends a simple GET to the server URL (without auth) to verify
        reachability.  Does NOT establish a full MCP session.
        """
        url = info.config.get("url")
        if not url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                logger.info(
                    "MCP warm-up: %s health check %d (user-aware, requires auth)",
                    name,
                    resp.status_code,
                )
        except Exception as exc:
            logger.warning("MCP warm-up: %s unreachable: %s", name, exc)

    async def shutdown(self) -> None:
        """Close all connections and stop background tasks."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # Disconnect all servers
        for name in list(self._connections.keys()):
            await self._disconnect(name)

        # Disconnect all user connections
        for key in list(self._user_connections.keys()):
            await self._disconnect_user(key)

        self._known_servers.clear()
        self._tool_cache = None
        logger.info("MCP Gateway: client manager shutdown")

    async def refresh_providers(self) -> bool:
        """Re-discover providers from plugin registry.

        Returns:
            True if the server list changed (additions/removals)
        """
        try:
            new_servers = discover_mcp_servers()
        except Exception as e:
            logger.error(f"MCP Gateway: provider discovery failed: {e}")
            return False

        old_names = set(self._known_servers.keys())
        new_names = set(new_servers.keys())

        added = new_names - old_names
        removed = old_names - new_names

        # Disconnect removed servers
        for name in removed:
            await self._disconnect(name)
            logger.info(f"MCP Gateway: server removed: {name}")

        # Update known servers
        self._known_servers = new_servers

        # Rebuild sanitized name mapping
        self._sanitized_to_original = {}
        for name in new_servers:
            sanitized = _sanitize_name(name)
            self._sanitized_to_original[sanitized] = name

        # Reset failed flag and circuit breaker on existing connections
        for name, conn in self._connections.items():
            if name in new_names and conn.failed:
                conn.failed = False
                self._get_circuit_breaker(name).reset()

        # Invalidate tool cache if servers changed
        if added or removed:
            self._tool_cache = None
            if added:
                logger.info(f"MCP Gateway: servers added: {sorted(added)}")
            return True

        return False

    def get_connection_health(self) -> dict[str, str]:
        """Return per-plugin health status based on connection state.

        Maps ``plugin_dir`` → status for the admin sidebar dot color:
        - ``"on"``      — connected and healthy (green)
        - ``"warn"``    — failed but circuit breaker not yet open (yellow)
        - ``"err"``     — circuit breaker open (red)
        - ``"unknown"`` — known but never connected yet (grey)
        """
        health: dict[str, str] = {}
        for name, info in self._known_servers.items():
            plugin = info.plugin_dir or name
            conn = self._connections.get(name)
            if conn and conn.failed:
                cb = self._get_circuit_breaker(name)
                health[plugin] = "err" if cb.state == "open" else "warn"
            elif conn and conn.connected:
                health[plugin] = "on"
            else:
                health[plugin] = "unknown"
        return health

    # Per-server timeout for connection + tool listing (seconds).
    _LIST_TOOLS_TIMEOUT: float = 10.0

    async def list_all_tools(
        self,
        unified_id: str | None = None,
        agent_name: str | None = None,
    ) -> list[dict]:
        """Aggregate tools from all known servers with namespacing.

        Connects to all servers **in parallel** (each with a timeout) so the
        total time is bounded by the slowest single server, not the sum.

        Args:
            unified_id: If provided, include user-aware servers with user credentials.
            agent_name: If provided, check agent-level credentials as fallback.

        Returns:
            List of tool dicts with namespaced names, ready for MCP tools/list response.
        """

        async def _gather_one(server_name: str, server_info: ServerInfo) -> list[dict]:
            """Gather namespaced tools from one server."""
            cb = self._get_circuit_breaker(server_name)
            if not cb.allow_request():
                logger.debug(
                    "MCP Gateway: skipping %s (circuit breaker %s)",
                    server_name,
                    cb.state,
                )
                return []

            try:
                if server_info.user_aware and unified_id:
                    creds = _get_user_credentials(
                        unified_id,
                        server_info.service_connection_id,
                        agent_name=agent_name,
                    )
                    if creds:
                        tools = await self._get_user_server_tools(
                            server_name, unified_id, creds
                        )
                    else:
                        # No per-user creds.  Fall back to the shared
                        # connection for stdio servers that have admin-
                        # configured accounts with baked-in credentials
                        # (e.g. Gmail per-account processes).
                        if "command" in server_info.config:
                            tools = await self._get_server_tools(server_name)
                        else:
                            return []
                elif not server_info.user_aware:
                    tools = await self._get_server_tools(server_name)
                else:
                    # user_aware but no unified_id: fall back to shared
                    # connection for stdio servers with admin-configured
                    # credentials (e.g. google-sheets with global SA)
                    if "command" in server_info.config:
                        tools = await self._get_server_tools(server_name)
                    else:
                        return []

                sanitized = _sanitize_name(server_name)
                result = []
                for tool in tools:
                    full_name = f"{sanitized}{NS_SEP}{tool['name']}"
                    if len(full_name) > 64:
                        full_name = full_name[:64]
                    entry = dict(tool)
                    entry["name"] = full_name
                    result.append(entry)
                return result
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                if isinstance(e, asyncio.CancelledError):
                    logger.debug(
                        "MCP Gateway: listing tools from %s cancelled: %s",
                        server_name,
                        e,
                    )
                    # Record failure so the circuit breaker cycles properly
                    # instead of getting stuck in half_open with in_flight=True
                    cb.record_failure()
                    return []
                logger.warning(
                    "MCP Gateway: failed to list tools from %s: %s",
                    server_name,
                    e,
                )
                cb.record_failure()
                return []

        # Run all servers in parallel, each with an individual timeout
        tasks = [
            asyncio.wait_for(_gather_one(name, info), timeout=self._LIST_TOOLS_TIMEOUT)
            for name, info in self._known_servers.items()
        ]
        server_names = list(self._known_servers.keys())
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_tools: list[dict] = []
        for i, result in enumerate(results):
            if isinstance(result, list):
                all_tools.extend(result)
            elif isinstance(result, BaseException):
                name = server_names[i]
                cb = self._get_circuit_breaker(name)
                cb.record_failure()
                logger.debug("MCP Gateway: %s timed out during tools/list", name)
        return all_tools

    async def call_tool(
        self,
        namespaced_name: str,
        arguments: dict,
        unified_id: str | None = None,
        agent_name: str | None = None,
    ) -> list[dict]:
        """Route a tool call to the correct server.

        Includes auto-reconnect: on connection failure, one reconnect
        attempt is made before giving up.  Circuit breaker state lives
        on the manager so it survives connection teardowns.

        Args:
            namespaced_name: Tool name in format "server_name__tool_name"
            arguments: Tool arguments
            unified_id: User identity for user-aware servers
            agent_name: Agent identity for agent-level credentials

        Returns:
            List of content blocks from the tool result

        Raises:
            ToolNotFoundError: If the server or tool cannot be found
        """
        server_name, tool_name = self._parse_namespaced(namespaced_name)

        if server_name not in self._known_servers:
            raise ToolNotFoundError(f"Server '{server_name}' not found")

        server_info = self._known_servers[server_name]
        cb = self._get_circuit_breaker(server_name)
        is_user_conn = server_info.user_aware and unified_id

        # Check circuit breaker before attempting call
        if not cb.allow_request():
            raise ToolNotFoundError(
                f"CIRCUIT BREAKER OPEN for server '{server_name}'. "
                f"Too many recent failures — requests blocked for "
                f"{int(cb.recovery_timeout)}s. The server will be retried "
                f"automatically after the cooldown period."
            )

        # Resolve credentials once for user-aware connections
        creds = None
        conn_key = None
        if is_user_conn:
            conn_key = f"{server_name}:{unified_id}"
            creds = _get_user_credentials(
                unified_id,
                server_info.service_connection_id,
                agent_name=agent_name,
            )
            if not creds:
                # No per-user credentials — fall back to shared connection.
                # Admin-configured servers (e.g. Gmail) have credentials
                # baked into the server process env.
                is_user_conn = False

        # Retry loop: original attempt + 1 reconnect
        for attempt in range(2):
            # Connect (or reconnect)
            if is_user_conn:
                conn = await self._ensure_user_connected(server_name, unified_id, creds)
            else:
                conn = await self._ensure_connected(server_name)

            if not conn or not conn.connected:
                raise ToolNotFoundError(f"Cannot connect to server '{server_name}'")

            conn.last_used = time.time()

            try:
                # Normalize argument keys: LLMs may convert camelCase to
                # snake_case (e.g. messageId → message_id).  Map back to the
                # original property names from the tool's inputSchema.
                normalized_args = _normalize_tool_args(arguments, tool_name, conn.tools)
                if normalized_args != arguments:
                    logger.info(
                        "MCP Gateway: normalized args for %s/%s: %s → %s",
                        server_name,
                        tool_name,
                        list(arguments.keys()),
                        list(normalized_args.keys()),
                    )
                else:
                    logger.debug(
                        "MCP Gateway: args for %s/%s (no normalization needed): %s",
                        server_name,
                        tool_name,
                        list(arguments.keys()),
                    )

                result = await conn.session.call_tool(tool_name, normalized_args)
                is_error = bool(_wire_field(result, "isError", False))
                if is_error:
                    logger.warning(
                        "MCP Gateway: tool %s/%s returned isError=True, content=%s",
                        server_name,
                        tool_name,
                        [
                            getattr(c, "text", str(c))[:200]
                            for c in (result.content or [])
                        ],
                    )
                # Convert result content to serializable dicts
                content = []
                for item in result.content:
                    if hasattr(item, "text"):
                        content.append({"type": "text", "text": item.text})
                    elif hasattr(item, "data"):
                        content.append(
                            {
                                "type": "image",
                                "data": item.data,
                                "mimeType": getattr(item, "mimeType", "image/png"),
                            }
                        )
                    elif hasattr(item, "resource"):
                        content.append(
                            {
                                "type": "resource",
                                "resource": {
                                    "uri": str(item.resource.uri),
                                    "text": getattr(item.resource, "text", ""),
                                    "mimeType": getattr(item.resource, "mimeType", ""),
                                },
                            }
                        )
                    else:
                        content.append({"type": "text", "text": str(item)})

                if is_error:
                    cb.record_soft_failure()
                else:
                    cb.record_success()
                logger.info(
                    f"MCP Gateway: tool call {server_name}/{tool_name} "
                    f"({len(content)} content blocks, isError={is_error})"
                )
                return content

            except KeyboardInterrupt:
                raise
            except BaseException as e:
                is_cancel = isinstance(e, asyncio.CancelledError)
                logger.error(
                    f"MCP Gateway: tool call failed {server_name}/{tool_name}: "
                    f"type={type(e).__name__} repr={e!r}"
                )
                # Disconnect — the session is likely broken
                if is_user_conn:
                    await self._disconnect_user(conn_key)
                else:
                    await self._disconnect(server_name)

                if attempt == 0:
                    # First failure: try one reconnect
                    logger.info(
                        f"MCP Gateway: attempting reconnect for "
                        f"{server_name}/{tool_name}"
                    )
                    continue

                # Second failure: give up.
                # CancelledError (SDK timeout) doesn't count against CB.
                if not is_cancel:
                    cb.record_failure()
                    if cb.state == "open":
                        logger.warning(
                            f"MCP Gateway: circuit breaker opened for {server_name}"
                        )
                        try:
                            from ui.services.notifications import NotificationService

                            asyncio.ensure_future(
                                NotificationService.get().create(
                                    category="mcp_failure",
                                    severity="warning",
                                    title=f"MCP server offline: {server_name}",
                                    message="Circuit breaker opened after repeated failures.",
                                    source=server_name,
                                )
                            )
                        except Exception:
                            pass
                    raise

                raise ToolNotFoundError(
                    f"Tool call cancelled for {server_name}/{tool_name}"
                ) from e

        raise ToolNotFoundError(f"Server '{server_name}' unreachable")

    def _parse_namespaced(self, namespaced_name: str) -> tuple[str, str]:
        """Parse a namespaced tool name into (original_server_name, tool_name).

        Split on first occurrence of NS_SEP ("__"), then resolve sanitized
        server name back to original via reverse mapping.
        """
        if NS_SEP not in namespaced_name:
            raise ToolNotFoundError(
                f"Invalid tool name format: '{namespaced_name}' "
                f"(expected 'server_name{NS_SEP}tool_name')"
            )

        idx = namespaced_name.index(NS_SEP)
        sanitized_name = namespaced_name[:idx]
        tool_name = namespaced_name[idx + len(NS_SEP) :]

        if not sanitized_name or not tool_name:
            raise ToolNotFoundError(f"Invalid tool name format: '{namespaced_name}'")

        # Resolve sanitized name to original server name
        original_name = self._sanitized_to_original.get(sanitized_name)
        if not original_name:
            # Try direct match (server names without special chars)
            if sanitized_name in self._known_servers:
                original_name = sanitized_name
            else:
                raise ToolNotFoundError(f"Server '{sanitized_name}' not found")

        return original_name, tool_name

    async def _get_server_tools(self, server_name: str) -> list[dict]:
        """Get tools from a server, connecting lazily if needed."""
        conn = await self._ensure_connected(server_name)
        if not conn or not conn.connected:
            return []

        conn.last_used = time.time()
        return conn.tools

    async def _ensure_connected(self, server_name: str) -> MCPServerConnection | None:
        """Ensure a server connection exists, creating it lazily if needed."""
        if server_name not in self._known_servers:
            return None

        server_info = self._known_servers[server_name]

        # Get or create connection entry
        if server_name not in self._connections:
            self._connections[server_name] = MCPServerConnection(
                server_info=server_info
            )

        conn = self._connections[server_name]

        # Check circuit breaker before attempting connection
        cb = self._get_circuit_breaker(server_name)
        if not cb.allow_request():
            logger.debug(
                f"MCP Gateway: circuit breaker open for {server_name}, skipping"
            )
            return None

        async with conn._lock:
            if conn.connected:
                return conn

            # Skip servers that already failed (wait for next refresh)
            if conn.failed:
                return None

            # Connect based on transport
            try:
                await self._connect(conn)
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                is_cancel = isinstance(e, asyncio.CancelledError)
                if is_cancel:
                    # MCP SDK cancel scope timeout — don't mark failed so
                    # the next request retries, don't count against CB.
                    logger.debug(
                        "MCP Gateway: connection to %s cancelled (SDK timeout): %s",
                        server_name,
                        e,
                    )
                else:
                    conn.failed = True
                    cb.record_failure()
                    logger.error(
                        "MCP Gateway: failed to connect to %s: %s",
                        server_name,
                        e,
                    )
                    if cb.state == "open":
                        try:
                            from ui.services.notifications import NotificationService

                            asyncio.ensure_future(
                                NotificationService.get().create(
                                    category="mcp_failure",
                                    severity="error",
                                    title=f"MCP connection failed: {server_name}",
                                    message=str(e)[:200],
                                    source=server_name,
                                )
                            )
                        except Exception:
                            pass
                return None

        return conn

    async def _connect(self, conn: MCPServerConnection) -> None:
        """Establish connection to an MCP server."""
        server_info = conn.server_info
        transport = server_info.transport

        try:
            if transport == "stdio":
                await self._connect_stdio(conn)
            elif transport == "sse":
                await self._connect_sse(conn)
            elif transport == "http":
                await self._connect_http(conn)
            else:
                logger.error(
                    f"MCP Gateway: unknown transport '{transport}' "
                    f"for {server_info.server_name}"
                )
                return

            # Initialize session and list tools
            if conn.session:
                await conn.session.initialize()
                response = await conn.session.list_tools()
                # Read the schema by its wire name rather than off the SDK
                # attribute. The protocol field is part of the spec and stable;
                # the Python attribute is not — mcp 2.0 renamed it to
                # input_schema, which made this line raise AttributeError and
                # took every server offline.
                conn.tools = [
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": _wire_field(tool, "inputSchema", {}),
                    }
                    for tool in response.tools
                ]
                conn.connected = True
                conn.last_used = time.time()
                logger.info(
                    f"MCP Gateway: connected to {server_info.server_name} "
                    f"({transport}, {len(conn.tools)} tools)"
                )

        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                logger.debug(
                    "MCP Gateway: connection to %s cancelled (SDK timeout)",
                    server_info.server_name,
                )
            else:
                logger.error(
                    "MCP Gateway: connection failed for %s (%s): %s",
                    server_info.server_name,
                    transport,
                    e,
                )
            await self._cleanup_connection(conn)
            raise

    async def _connect_stdio(self, conn: MCPServerConnection) -> None:
        """Connect to a stdio MCP server."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        config = conn.server_info.config
        params = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env"),
            cwd=config.get("cwd"),
        )

        # stdio_client is an async context manager that yields (read, write)
        conn.transport_ctx = stdio_client(params)
        read_stream, write_stream = await conn.transport_ctx.__aenter__()
        conn.read_stream = read_stream
        conn.write_stream = write_stream

        # Create session
        conn.session = ClientSession(read_stream, write_stream)
        await conn.session.__aenter__()

    async def _connect_sse(self, conn: MCPServerConnection) -> None:
        """Connect to an SSE MCP server."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        config = conn.server_info.config
        url = config["url"]
        headers = config.get("headers")

        conn.transport_ctx = sse_client(url=url, headers=headers)
        read_stream, write_stream = await conn.transport_ctx.__aenter__()
        conn.read_stream = read_stream
        conn.write_stream = write_stream

        conn.session = ClientSession(read_stream, write_stream)
        await conn.session.__aenter__()

    async def _connect_http(self, conn: MCPServerConnection) -> None:
        """Connect to an HTTP (Streamable HTTP) MCP server."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        config = conn.server_info.config
        url = config["url"]
        headers = config.get("headers")

        conn.transport_ctx = streamablehttp_client(url=url, headers=headers)
        read_stream, write_stream, _ = await conn.transport_ctx.__aenter__()
        conn.read_stream = read_stream
        conn.write_stream = write_stream

        conn.session = ClientSession(read_stream, write_stream)
        await conn.session.__aenter__()

    async def _disconnect(self, server_name: str) -> None:
        """Disconnect from a server and clean up resources."""
        conn = self._connections.pop(server_name, None)
        if conn:
            await self._cleanup_connection(conn)
            logger.info(f"MCP Gateway: disconnected from {server_name}")

    async def _cleanup_connection(self, conn: MCPServerConnection) -> None:
        """Clean up a connection's resources."""
        conn.connected = False
        conn.tools = []

        # Close session
        if conn.session:
            try:
                await conn.session.__aexit__(None, None, None)
            except Exception:
                pass
            conn.session = None

        # Close transport with timeout to prevent indefinite hangs
        # when anyio _deliver_cancellation retries on stuck tasks
        if conn.transport_ctx:
            try:
                await asyncio.wait_for(
                    conn.transport_ctx.__aexit__(None, None, None),
                    timeout=10,
                )
            except Exception:
                pass
            conn.transport_ctx = None

        conn.read_stream = None
        conn.write_stream = None

    async def _get_user_server_tools(
        self, server_name: str, unified_id: str, credentials: dict
    ) -> list[dict]:
        """Get tools from a user-aware server, connecting with user creds if needed."""
        conn = await self._ensure_user_connected(server_name, unified_id, credentials)
        if not conn or not conn.connected:
            return []
        conn.last_used = time.time()
        return conn.tools

    async def _ensure_user_connected(
        self, server_name: str, unified_id: str, credentials: dict
    ) -> MCPServerConnection | None:
        """Ensure a per-user server connection exists."""
        if server_name not in self._known_servers:
            return None

        server_info = self._known_servers[server_name]
        conn_key = f"{server_name}:{unified_id}"

        if conn_key not in self._user_connections:
            # Build user-specific config from provider
            user_config = self._build_user_config(server_info, unified_id, credentials)
            user_info = ServerInfo(
                server_name=server_name,
                config=user_config,
                transport=server_info.transport,
                provider_name=server_info.provider_name,
                allowed_tools=server_info.allowed_tools,
                user_aware=True,
                service_connection_id=server_info.service_connection_id,
            )
            self._user_connections[conn_key] = MCPServerConnection(
                server_info=user_info
            )

        conn = self._user_connections[conn_key]

        async with conn._lock:
            if conn.connected:
                return conn
            if conn.failed:
                # Allow retry after 60s cooldown (credentials may have been updated)
                if time.time() - conn.failed_at < 60:
                    return None
                # Reset failed state for retry
                conn.failed = False
                conn.failed_at = 0
                logger.info(
                    "MCP Gateway: retrying connection %s for user %s "
                    "(cooldown expired)",
                    server_name,
                    unified_id,
                )
                # Rebuild config with fresh credentials
                user_config = self._build_user_config(
                    self._known_servers[server_name], unified_id, credentials
                )
                conn.server_info = ServerInfo(
                    server_name=server_name,
                    config=user_config,
                    transport=self._known_servers[server_name].transport,
                    provider_name=self._known_servers[server_name].provider_name,
                    allowed_tools=self._known_servers[server_name].allowed_tools,
                    user_aware=True,
                    service_connection_id=self._known_servers[
                        server_name
                    ].service_connection_id,
                )

            try:
                await self._connect(conn)
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                is_cancel = isinstance(e, asyncio.CancelledError)
                if is_cancel:
                    # SDK timeout — don't mark failed, retry on next request
                    logger.debug(
                        "MCP Gateway: user connection to %s cancelled "
                        "(SDK timeout): %s",
                        server_name,
                        e,
                    )
                else:
                    conn.failed = True
                    conn.failed_at = time.time()
                    # Log sub-exceptions from TaskGroup for debugging
                    sub = ""
                    if hasattr(e, "exceptions"):
                        sub = "; ".join(str(se) for se in e.exceptions)
                    err_str = f"{e} {sub}".lower()
                    logger.error(
                        f"MCP Gateway: failed to connect user {unified_id} "
                        f"to {server_name}: {e}" + (f" | sub: {sub}" if sub else ""),
                        exc_info=True,
                    )
                    # Only surface the failure as a user-facing notification
                    # when it's an actionable auth problem — not for
                    # transient network / TaskGroup-wrapped errors that the
                    # next request typically recovers from on its own.
                    is_auth_error = "401" in err_str or "unauthorized" in err_str
                    if is_auth_error:
                        conn_id = self._known_servers[server_name].service_connection_id
                        if conn_id:
                            _mark_token_expired(unified_id, conn_id)
                        try:
                            from ui.services.notifications import NotificationService

                            asyncio.ensure_future(
                                NotificationService.get().create(
                                    category="oauth_expired",
                                    severity="error",
                                    title=f"Connection failed: {server_name}",
                                    message=str(e)[:200],
                                    source=server_name,
                                    user_id=unified_id,
                                )
                            )
                        except Exception:
                            pass
                return None

        return conn

    def _build_user_config(
        self, server_info: ServerInfo, unified_id: str, credentials: dict
    ) -> dict:
        """Build server config with per-user credentials."""
        # Try loading the provider to get user-specific config
        try:
            import json

            from core.mcp_gateway.provider_loader import _load_provider_class
            from core.registry import get_plugin_path
            from ui.plugin_helpers import load_plugin_config

            # Use get_plugin_path() which resolves external plugin dirs
            # (GRIDBEAR_PLUGIN_PATHS), not just the core plugins/ directory
            plugin_dir = server_info.plugin_dir or server_info.provider_name
            plugin_path = get_plugin_path(plugin_dir)
            manifest_path = plugin_path / "manifest.json" if plugin_path else None
            if manifest_path and manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)

                provider_cls = _load_provider_class(plugin_dir, manifest)
                if provider_cls and hasattr(provider_cls, "get_user_server_config"):
                    plugin_config = load_plugin_config(plugin_dir)
                    provider = provider_cls(plugin_config)
                    return provider.get_user_server_config(unified_id, credentials)
        except Exception as e:
            logger.warning(
                "MCP Gateway: failed to get user config from provider: %s",
                e,
                exc_info=True,
            )

        # Fallback: use global config with injected auth header
        base_config = dict(server_info.config)
        token = (
            credentials.get("access_token")
            or credentials.get("token")
            or credentials.get("api_key")
        )
        if token and "url" in base_config:
            headers = dict(base_config.get("headers") or {})
            headers["Authorization"] = f"Bearer {token}"
            base_config["headers"] = headers
        return base_config

    async def _disconnect_user(self, conn_key: str) -> None:
        """Disconnect a user-specific connection."""
        conn = self._user_connections.pop(conn_key, None)
        if conn:
            await self._cleanup_connection(conn)
            logger.info(f"MCP Gateway: disconnected user connection {conn_key}")

    async def invalidate_user_connections(
        self, unified_id: str, server_name: str | None = None
    ) -> int:
        """Invalidate cached user connections so they reconnect with fresh credentials.

        Called when a user updates or removes their service credentials (e.g. from /me).
        If server_name is given, only that server's connection is invalidated;
        otherwise all connections for the user are invalidated.

        Returns the number of connections invalidated.
        """
        targets = []
        for key in list(self._user_connections.keys()):
            parts = key.split(":", 1)
            if len(parts) == 2 and parts[1] == unified_id:
                if server_name is None or parts[0] == server_name:
                    targets.append(key)

        for key in targets:
            await self._disconnect_user(key)

        if targets:
            logger.info(
                "MCP Gateway: invalidated %d user connection(s) for %s%s",
                len(targets),
                unified_id,
                f" (server={server_name})" if server_name else "",
            )
        return len(targets)

    async def _cleanup_loop(self) -> None:
        """Periodically check and disconnect idle connections."""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL)
                now = time.time()

                # Clean up global connections (skip stdio — reconnect is fragile)
                idle_servers = [
                    name
                    for name, conn in self._connections.items()
                    if conn.connected
                    and (now - conn.last_used) > IDLE_TIMEOUT
                    and conn.server_info.transport != "stdio"
                ]

                for name in idle_servers:
                    logger.info(f"MCP Gateway: disconnecting idle server: {name}")
                    await self._disconnect(name)

                # Clean up user connections
                idle_user = [
                    key
                    for key, conn in self._user_connections.items()
                    if conn.connected and (now - conn.last_used) > IDLE_TIMEOUT
                ]

                for key in idle_user:
                    logger.info(
                        f"MCP Gateway: disconnecting idle user connection: {key}"
                    )
                    await self._disconnect_user(key)

                # Health check: retry half-open circuit breakers
                for name, conn in self._connections.items():
                    cb = self._get_circuit_breaker(name)
                    if cb.state == "half_open":
                        logger.info(
                            f"MCP Gateway: testing half-open circuit for {name}"
                        )
                        conn.failed = False
                        try:
                            await self._connect(conn)
                            cb.record_success()
                            self._tool_cache = None  # refresh tool list
                            logger.info(
                                f"MCP Gateway: circuit breaker closed for {name}"
                            )
                        except Exception:
                            conn.failed = True
                            cb.record_failure()

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"MCP Gateway: cleanup error: {e}")
