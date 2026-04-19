import atexit
import os
import signal
from asyncio import get_running_loop
from pathlib import Path

import anyio._backends._asyncio as _anyio_asyncio  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from config.logging_config import logger

# ── Throttle anyio _deliver_cancellation (CPU leak fix) ─────────────
# When a CancelScope cancels tasks blocked on slow I/O (MCP SSE/stdio),
# anyio retries via call_soon() — a tight loop burning 100% CPU.
# Replacing with call_later(1.0) adds 1s between retries (~0% CPU).
_orig_deliver_cancellation = _anyio_asyncio.CancelScope._deliver_cancellation


def _throttled_deliver_cancellation(self, origin):
    result = _orig_deliver_cancellation(self, origin)
    if origin is self and self._cancel_handle is not None:
        self._cancel_handle.cancel()
        self._cancel_handle = get_running_loop().call_later(
            1.0, self._deliver_cancellation, origin
        )
    return result


_anyio_asyncio.CancelScope._deliver_cancellation = _throttled_deliver_cancellation
# ────────────────────────────────────────────────────────────────────
from core.__version__ import __version__
from core.mcp_gateway.server import router as mcp_gateway_router
from core.oauth2.discovery import router as oauth2_discovery_router
from core.oauth2.server import router as oauth2_server_router
from core.oauth2.server import set_db as set_oauth2_db
from core.plugin_paths import PluginPathResolver, build_plugin_dirs
from core.registry import set_path_resolver
from core.rest_api.router import router as rest_api_router
from ui.auth.database import init_auth_db
from ui.auth.session import session_manager
from ui.config_manager import ConfigManager
from ui.csrf import CSRFMiddleware
from ui.plugin_admin import PluginAdminRegistry
from ui.routes import (
    agents,
    auth,
    chat_api,
    chat_proxy,
    companies,
    me,
    memory,
    notifications,
    permissions,
    plugins,
    tools,
    users,
    ws_chat,
)
from ui.routes import languages as languages_routes
from ui.routes import (
    oauth2 as oauth2_admin,
)
from ui.routes import rest_api as rest_api_routes
from ui.routes import secrets as secrets_routes
from ui.routes import settings as settings_routes
from ui.routes import themes as themes_routes
from ui.routes import vault as vault_routes
from ui.routes.auth import require_login

BASE_DIR = Path(__file__).resolve().parent.parent
ADMIN_DIR = Path(__file__).resolve().parent
SESSION_SECRET_PATH = BASE_DIR / "data" / ".session_secret"


def get_or_create_session_secret() -> str:
    """Get or create a persistent session secret."""
    import secrets as stdlib_secrets

    SESSION_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)

    if SESSION_SECRET_PATH.exists():
        return SESSION_SECRET_PATH.read_text().strip()

    secret = stdlib_secrets.token_hex(32)
    SESSION_SECRET_PATH.write_text(secret)
    SESSION_SECRET_PATH.chmod(0o600)
    return secret


app = FastAPI(
    title="GridBear Admin",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

_path_resolver = PluginPathResolver(build_plugin_dirs(BASE_DIR))
set_path_resolver(_path_resolver)

from core.models_registry import ModelsRegistry
from core.registry import set_models_registry

_models_registry = ModelsRegistry()
set_models_registry(_models_registry)

plugin_registry = PluginAdminRegistry(
    path_resolver=_path_resolver,
)

# HTTPS_ONLY: Set to "true" in production behind HTTPS proxy
HTTPS_ONLY = os.getenv("ADMIN_HTTPS_ONLY", "false").lower() == "true"

# CSRF middleware must be added BEFORE SessionMiddleware so session processes first
app.add_middleware(CSRFMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_session_secret(),
    session_cookie="gridbear_admin_session",
    max_age=3600,
    https_only=HTTPS_ONLY,
    same_site="strict" if HTTPS_ONLY else "lax",
)

# Only trust proxy headers from known networks (Docker internal, localhost).
# Override with TRUSTED_PROXY_HOSTS env var (comma-separated) if needed.
_trusted_hosts = os.getenv(
    "TRUSTED_PROXY_HOSTS", "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
).split(",")
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_hosts)

from starlette.middleware.gzip import GZipMiddleware

app.add_middleware(GZipMiddleware, minimum_size=500)


# Mount theme static directories BEFORE the general /static mount
# (more specific paths must be registered first in Starlette)
_all_manifests = _path_resolver.discover_all()
for _pname, _manifest in _all_manifests.items():
    if _manifest and _manifest.get("type") == "theme":
        _theme_path = _path_resolver.resolve(_pname)
        if _theme_path:
            _static_dir = _theme_path / "static"
            if _static_dir.is_dir():
                app.mount(
                    f"/static/theme/{_pname}",
                    StaticFiles(directory=_static_dir),
                    name=f"theme-static-{_pname}",
                )

# Agent avatars live under data/ so they persist via the ./data:/app/data
# bind-mount. Keeping them under ui/static would require a dedicated mount
# in docker-compose.yml that not every existing deploy has — see issue where
# avatars vanished on container rebuild for installs predating 8c229bd.
_AVATARS_DIR = BASE_DIR / "data" / "avatars"
_AVATARS_DIR.mkdir(parents=True, exist_ok=True)

_legacy_avatars = ADMIN_DIR / "static" / "avatars"
if (
    _legacy_avatars.is_dir()
    and any(_legacy_avatars.iterdir())
    and not any(_AVATARS_DIR.iterdir())
):
    import shutil

    for _f in _legacy_avatars.iterdir():
        if _f.is_file():
            shutil.move(str(_f), str(_AVATARS_DIR / _f.name))
    logger.info("Migrated agent avatars from ui/static/avatars to data/avatars")

app.mount("/static/avatars", StaticFiles(directory=_AVATARS_DIR), name="avatars")
app.mount("/static", StaticFiles(directory=ADMIN_DIR / "static"), name="static")

from ui.jinja_env import rebuild_template_loader, templates  # noqa: E402

# Rebuild template loader with theme overrides
rebuild_template_loader()


def get_enabled_plugins_by_type() -> dict:
    """Get enabled plugins grouped by type.

    Uses the path resolver to find plugins across multiple directories.
    """
    from ui.plugin_helpers import get_enabled_plugins

    result = {
        "channels": [],
        "services": [],
        "mcp": [],
        "runners": [],
        "themes": [],
    }

    enabled = get_enabled_plugins()
    if not enabled:
        return result

    all_manifests = _path_resolver.discover_all()

    # Collect providers to hide: plugins whose base (via "provides") is
    # also enabled get hidden from the sidebar (shown on the base page).
    enabled_set = set(enabled)
    providers_to_hide = set()
    for plugin_name in enabled:
        manifest = all_manifests.get(plugin_name)
        if manifest and manifest.get("provides"):
            base = manifest["provides"]
            if base in enabled_set:
                providers_to_hide.add(plugin_name)

    for plugin_name in enabled:
        if plugin_name in providers_to_hide:
            continue
        manifest = all_manifests.get(plugin_name)
        if manifest is None:
            continue
        plugin_type = manifest.get("type", "")
        if plugin_type == "channel":
            result["channels"].append(plugin_name)
        elif plugin_type == "service":
            result["services"].append(plugin_name)
        elif plugin_type == "mcp":
            result["mcp"].append(plugin_name)
        elif plugin_type == "runner":
            result["runners"].append(plugin_name)
        elif plugin_type == "theme":
            result["themes"].append(plugin_name)

    return result


class _ContextMiddleware:
    """Pure ASGI middleware replacing three @app.middleware("http") functions.

    Handles: user auth, tenant context, i18n, role-based redirects,
    plugin menus, context skills, and content-length cleanup.

    Avoids BaseHTTPMiddleware's anyio TaskGroup which causes
    _deliver_cancellation CPU loops on connection close.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = scope.get("path", "")

        # ── 1. User + tenant context (from add_plugin_context) ──────
        from ui.auth.session import get_current_user

        scope.setdefault("state", {})
        request.state.active_company_id = None
        request.state.active_company_name = None
        request.state.user_companies = []
        request.state.current_user = get_current_user(request)

        user = request.state.current_user
        if user:
            try:
                from core.models.company import Company
                from core.models.company_user import CompanyUser
                from core.tenant import SUPERADMIN_BYPASS, clear_tenant, set_tenant

                cu_rows = CompanyUser.search_sync(
                    [("user_id", "=", user["id"])],
                )
                if cu_rows:
                    company_ids = [row["company_id"] for row in cu_rows]
                    companies = Company.search_sync(
                        [("id", "in", company_ids)],
                    )
                    name_map = {c["id"]: c["name"] for c in companies}

                    company_list = []
                    default_company_id = None
                    for row in cu_rows:
                        cid = row["company_id"]
                        entry = dict(row)
                        entry["company_name"] = name_map.get(cid, f"Company #{cid}")
                        company_list.append(entry)
                        if row.get("is_default"):
                            default_company_id = cid

                    if default_company_id is None:
                        default_company_id = company_ids[0]

                    active_company_id = default_company_id

                    if user.get("is_superadmin"):
                        session_override = request.session.get("active_company_id")
                        if session_override is not None:
                            active_company_id = int(session_override)

                    set_tenant(active_company_id, tuple(company_ids))
                    request.state.active_company_id = active_company_id
                    request.state.user_companies = company_list
                    request.state.active_company_name = name_map.get(active_company_id)
                elif user.get("is_superadmin"):
                    set_tenant(SUPERADMIN_BYPASS)
            except Exception as exc:
                logger.debug("Tenant context setup: %s", exc)

        # ── 2. Role-based access enforcement ────────────────────────
        public_prefixes = (
            "/auth/",
            "/static/",
            "/me",
            "/oauth2/",
            "/ws/",
            "/.well-known/",
            "/notifications",
        )
        is_public = any(path.startswith(p) for p in public_prefixes) or path == "/mcp"

        if not is_public and user and not user.get("is_superadmin"):
            try:
                from core.tenant import clear_tenant

                clear_tenant()
            except Exception:
                pass
            response = RedirectResponse(url="/me", status_code=303)
            await response(scope, receive, send)
            return

        # ── 3. Plugin menus + enabled plugins ───────────────────────
        request.state.plugins = get_enabled_plugins_by_type()
        request.state.plugin_menus = plugin_registry.discover_plugin_menus()

        # ── 4. i18n language (from i18n_middleware) ──────────────────
        from core.i18n import resolve_language, set_language

        accept = ""
        for key, value in scope.get("headers", []):
            if key == b"accept-language":
                accept = value.decode("latin-1")
                break
        lang = resolve_language(user=user, accept_language=accept)
        set_language(lang)

        # ── 5. Context skill injection (from inject_context_skill) ──
        stripped_path = path.rstrip("/")
        if stripped_path.startswith("/plugin/"):
            parts = stripped_path.split("/")
            if len(parts) >= 3:
                plugin_name = parts[2]
                try:
                    from core.registry import get_database

                    db = get_database()
                    if db:
                        rows = await db.fetch_all(
                            "SELECT id, title, prompt FROM app.skills "
                            "WHERE plugin_name = %s AND category = %s",
                            (plugin_name, "context"),
                        )
                        if rows:
                            s = rows[0]
                            prompt = s.get("prompt", "") or ""
                            request.state.context_skill = {
                                "id": s["id"],
                                "title": s.get("title", ""),
                                "preview": (prompt[:120] + "...")
                                if len(prompt) > 120
                                else prompt,
                            }
                except Exception as exc:
                    logger.debug("Context skill lookup for %s: %s", plugin_name, exc)

        # ── 6. Wrap send to strip content-length ────────────────────
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"content-length"
                ]
                message = {**message, "headers": headers}
            await send(message)

        # ── 7. Call downstream, then clear tenant ───────────────────
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                from core.tenant import clear_tenant

                clear_tenant()
            except Exception:
                pass


app.add_middleware(_ContextMiddleware)


def _get_plugin_health(plugins: dict) -> dict[str, str]:
    """Check health of all enabled plugins.

    Returns ``{plugin_name: status}`` where *status* is one of
    ``"on"`` (healthy), ``"warn"`` (degraded/missing key), ``"err"``
    (failed/circuit-open), ``"off"`` (disabled/unknown).
    The dot CSS classes in theme-nordic map directly to these suffixes.

    Health sources:
    - **All types**: missing required secrets from ``config_schema``
    - **MCP plugins**: gateway connection state (connected/failed/circuit-open)
    - **Claude runner**: CLI credentials expiry check
    """
    from core.registry import get_path_resolver
    from ui.secrets_manager import secrets_manager

    health: dict[str, str] = {}
    try:
        resolver = get_path_resolver()
        if not resolver:
            return health
        all_manifests = resolver.discover_all()
    except Exception:
        return health

    # Check required secrets for ALL plugin types
    check_names = (
        plugins.get("runners", [])
        + plugins.get("channels", [])
        + plugins.get("services", [])
        + plugins.get("mcp", [])
    )
    for plugin_name in check_names:
        manifest = all_manifests.get(plugin_name)
        if not manifest:
            continue
        config_schema = manifest.get("config_schema", {})
        missing = False
        for field_def in config_schema.values():
            if not isinstance(field_def, dict):
                continue
            if field_def.get("type") == "secret" and field_def.get("required"):
                env_key = field_def.get("env", "")
                if env_key:
                    try:
                        if not secrets_manager.get(env_key, fallback_env=True):
                            missing = True
                            break
                    except Exception:
                        pass
        health[plugin_name] = "warn" if missing else "on"

    # MCP plugins: overlay gateway connection state (worse status wins)
    try:
        from core.mcp_gateway.server import get_client_manager

        cm = get_client_manager()
        if cm:
            gw_health = cm.get_connection_health()
            for plugin_name, gw_status in gw_health.items():
                current = health.get(plugin_name, "on")
                # err > warn > on  — keep the worst status
                if _health_severity(gw_status) > _health_severity(current):
                    health[plugin_name] = gw_status
    except Exception:
        pass

    # Map "unknown" → "off" for CSS (dot-off = grey, no dot-unknown class)
    for name, status in health.items():
        if status == "unknown":
            health[name] = "off"

    # Emit notifications for degraded plugins (deduplicated, best-effort)
    _notify_degraded_plugins(health)

    return health


_HEALTH_SEVERITY = {"on": 0, "unknown": 1, "warn": 2, "err": 3, "off": 4}


def _health_severity(status: str) -> int:
    return _HEALTH_SEVERITY.get(status, 0)


def _notify_degraded_plugins(health: dict[str, str]) -> None:
    """Create an admin notification for each degraded plugin.

    Uses the NotificationService's built-in deduplication (60 min window)
    so repeated page loads don't spam.  Runs fire-and-forget in the
    background via ``asyncio.ensure_future``.

    Also auto-resolves notifications for plugins that returned to healthy.
    """
    import asyncio

    degraded = {
        name: status for name, status in health.items() if status in ("warn", "err")
    }
    healthy = [name for name, status in health.items() if status == "on"]

    severity_map = {"warn": "warning", "err": "error"}

    try:
        from ui.services.notifications import NotificationService

        svc = NotificationService.get()

        # Auto-resolve notifications for plugins that are now healthy
        if healthy:
            asyncio.ensure_future(svc.resolve_by_source("plugin_health", healthy))

        for name, status in degraded.items():
            asyncio.ensure_future(
                svc.create(
                    category="plugin_health",
                    severity=severity_map.get(status, "warning"),
                    title=f"Plugin degraded: {name}",
                    message=(
                        "Missing required secrets or credentials."
                        if status == "warn"
                        else "Connection failed — circuit breaker active."
                    ),
                    source=name,
                    action_url=f"/plugins/{name}",
                )
            )
    except Exception:
        pass


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context with enabled plugins and menus."""
    plugins = getattr(request.state, "plugins", get_enabled_plugins_by_type())
    plugin_menus = getattr(request.state, "plugin_menus", [])
    return {
        "request": request,
        "version": __version__,
        "enabled_channels": plugins.get("channels", []),
        "enabled_services": plugins.get("services", []),
        "enabled_mcp": plugins.get("mcp", []),
        "enabled_runners": plugins.get("runners", []),
        "plugin_menus": plugin_menus,
        "plugin_health": _get_plugin_health(plugins),
        **kwargs,
    }


app.include_router(ws_chat.router, tags=["webchat"])  # /ws/chat (WebSocket, no prefix)
# Plugin portal routes registered in startup event (requires ORM for get_enabled_plugins)
app.include_router(me.router, tags=["user-portal"])  # /me/* (before auth prefix)
app.include_router(chat_api.router, tags=["chat-api"])  # /me/chat/api/*
app.include_router(chat_proxy.router, tags=["chat-proxy"])  # /api/proxy/chat
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(agents.router, prefix="/agents", tags=["agents"])
app.include_router(tools.router, prefix="/tools", tags=["tools"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(companies.router, prefix="/companies", tags=["companies"])
app.include_router(permissions.router, prefix="/permissions", tags=["permissions"])
app.include_router(memory.router, prefix="/memory", tags=["memory"])
app.include_router(
    notifications.router, prefix="/notifications", tags=["notifications"]
)

from ui.routes import vault_api

app.include_router(vault_api.router)


# ── Plugin admin routes (MUST be registered before app startup) ───────
# Starlette compiles the routing table when building the middleware stack.
# Routes added via include_router() during startup events are NOT reachable.
# We register plugin routes here at module level using a direct psycopg
# query (ORM is not yet initialized).
def _register_plugin_routes():
    """Register plugin-specific routes BEFORE the generic catch-all."""
    try:
        plugin_registry.register_plugin_routes_sync(app)
    except Exception as exc:
        logger.warning("Early plugin route registration failed: %s", exc)
    # Generic catch-all AFTER plugin-specific routes.
    # plugins.router has prefix="/plugins" built-in — include WITHOUT
    # prefix so Starlette doesn't create a Mount that shadows plugin routes.
    app.include_router(plugins.router, tags=["plugins"])


_register_plugin_routes()

app.include_router(secrets_routes.router)
app.include_router(settings_routes.router)
app.include_router(languages_routes.router)
app.include_router(rest_api_routes.router, prefix="/rest-api", tags=["rest-api"])
app.include_router(themes_routes.router, prefix="/themes", tags=["themes"])
app.include_router(vault_routes.router)
# OAuth2 well-known endpoints (must be at root level)
app.include_router(oauth2_discovery_router, tags=["oauth2"])
# OAuth2 authorization server
app.include_router(oauth2_server_router, prefix="/oauth2", tags=["oauth2"])
# OAuth2 admin UI (client management)
app.include_router(oauth2_admin.router, tags=["oauth2"])
# MCP Gateway (Streamable HTTP transport)
app.include_router(mcp_gateway_router, tags=["mcp"])
# REST API (generic CRUD, Bearer token auth)
app.include_router(rest_api_router, tags=["rest-api"])


# ── Health check (used by Docker healthcheck) ─────────────────────
@app.get("/health", include_in_schema=False)
async def health_check():
    """Lightweight health probe for Docker healthcheck."""
    return {"status": "ok"}


def get_agents_count() -> int:
    """Count configured agents."""
    try:
        from core.models.agent_config import AgentConfigRecord

        return len(AgentConfigRecord.search_sync([("is_active", "=", True)]))
    except Exception:
        return 0


def get_agents_list() -> list[dict]:
    """Return list of agent metadata from DB."""
    agents = []

    # Read global default_runner from SystemConfig
    global_default_runner = None
    try:
        from core.system_config import SystemConfig

        global_default_runner = SystemConfig.get_param_sync("default_runner")
    except Exception:
        pass

    try:
        from core.models.agent_config import AgentConfigRecord

        records = AgentConfigRecord.search_sync([("is_active", "=", True)])
    except Exception as exc:
        logger.debug("Failed to load agent configs from DB: %s", exc)
        return agents

    for record in sorted(records, key=lambda r: r.get("id", "")):
        data = dict(record)
        channels = list((data.get("channels") or {}).keys())
        runner = data.get("runner") or global_default_runner
        agents.append(
            {
                "name": data.get("name", data.get("id", "")),
                "id": data.get("id", ""),
                "runner": runner,
                "channels": channels,
            }
        )
    return agents


async def get_mcp_servers_info() -> list[dict]:
    """Get MCP servers with tool counts from the local gateway client manager.

    The MCP gateway runs in this (UI) container. Uses cached connection data
    when available (no new connections forced).
    """
    from core.mcp_gateway.server import get_client_manager
    from ui.plugin_helpers import get_enabled_plugins

    all_manifests = _path_resolver.discover_all()

    # Build tool counts from gateway cached connections (runs in this container)
    cm = get_client_manager()
    gateway_tool_counts: dict[str, int] = {}
    # Also aggregate by provider_name for multi-account plugins (e.g. gmail)
    provider_tool_counts: dict[str, int] = {}
    if cm:
        for server_name, conn in cm._connections.items():
            if conn.connected:
                gateway_tool_counts[server_name] = len(conn.tools)
        for conn_key, conn in cm._user_connections.items():
            if conn.connected:
                server_name = (
                    conn_key.split(":", 1)[-1] if ":" in conn_key else conn_key
                )
                existing = gateway_tool_counts.get(server_name, 0)
                gateway_tool_counts[server_name] = max(existing, len(conn.tools))

        # Aggregate tool counts by provider_name (handles per_account plugins
        # where server names are e.g. gmail-user@example.com but plugin is gmail)
        for server_name, info in cm._known_servers.items():
            count = gateway_tool_counts.get(server_name, 0)
            if count > 0:
                provider_tool_counts[info.provider_name] = (
                    provider_tool_counts.get(info.provider_name, 0) + count
                )

    enabled = get_enabled_plugins()
    servers = []
    for name in enabled:
        manifest = all_manifests.get(name)
        if manifest and manifest.get("type") == "mcp":
            mcp_name = manifest.get("mcp_server_name", name)
            tool_count = gateway_tool_counts.get(mcp_name, 0)
            if tool_count == 0:
                # Fallback: multi-account plugins store tools under
                # per-account server names, aggregate via provider_name
                tool_count = provider_tool_counts.get(name, 0)
            servers.append(
                {
                    "name": name,
                    "tool_count": tool_count,
                    "display_name": manifest.get("display_name", name),
                }
            )
    return servers


async def _fetch_bot_uptime() -> str:
    """Fetch uptime from the bot's /api/health endpoint."""
    import aiohttp

    gridbear_url = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
    secret = os.getenv("INTERNAL_API_SECRET", "")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{gridbear_url}/api/health",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    elapsed = data.get("data", {}).get("uptime_seconds", 0)
                    days = int(elapsed // 86400)
                    hours = int((elapsed % 86400) // 3600)
                    if days > 0:
                        return f"{days}d {hours}h"
                    minutes = int((elapsed % 3600) // 60)
                    return f"{hours}h {minutes}m"
    except Exception:
        pass
    return "restarting..."


async def get_system_info() -> dict:
    """Gather system health information."""
    from core.registry import get_database

    info = {
        "db_healthy": False,
        "plugin_count": len(_path_resolver.discover_all()),
        "version": __version__,
        "uptime": "",
        "orm_model_count": 0,
    }

    # DB health check
    db = get_database()
    if db:
        try:
            row = await db.fetch_one("SELECT 1 AS ok")
            info["db_healthy"] = row is not None
        except Exception:
            pass

    # ORM model count
    try:
        from core.orm import Registry as ORMRegistry

        info["orm_model_count"] = len(ORMRegistry._models)
    except Exception:
        pass

    # Bot uptime (from gridbear container, not UI process)
    info["uptime"] = await _fetch_bot_uptime()

    return info


async def get_tool_usage_24h() -> int:
    """Count tool usage records from the last 24 hours."""
    from core.registry import get_database

    db = get_database()
    if not db:
        return 0
    try:
        row = await db.fetch_one(
            "SELECT count(*) AS cnt FROM tool_usage "
            "WHERE called_at >= NOW() - INTERVAL '24 hours'"
        )
        return row["cnt"] if row else 0
    except Exception:
        return 0


async def get_recent_log_entries(limit: int = 20) -> list[dict]:
    """Fetch recent WARNING/ERROR log entries from the database."""
    from core.registry import get_database

    db = get_database()
    if not db:
        return []
    try:
        rows = await db.fetch_all(
            "SELECT level, logger_name, message, created_at "
            "FROM admin.log_entries "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: dict = Depends(require_login)):
    from ui.utils.channels import get_available_channels

    config = ConfigManager()
    plugins = get_enabled_plugins_by_type()

    # Build channels list with user counts
    channels = get_available_channels()
    for ch in channels:
        users = config.get_channel_users(ch["name"])
        ch["user_count"] = len(users.get("ids", [])) + len(users.get("usernames", []))
        ch["users"] = users

    # Enriched dashboard data — always provided so theme templates
    # that use these variables don't hit Undefined errors.
    extra_dashboard = {
        "agents_list": get_agents_list(),
        "mcp_servers": await get_mcp_servers_info(),
        "system_info": await get_system_info(),
        "tool_usage_24h": await get_tool_usage_24h(),
        "log_entries": await get_recent_log_entries(),
    }

    return templates.TemplateResponse(
        "dashboard.html",
        get_template_context(
            request,
            channels=channels,
            gmail_accounts=config.get_gmail_accounts(),
            user_identities=config.get_user_identities(),
            agents_count=get_agents_count(),
            # Exposed so the template can warn when the configured public
            # URL doesn't match where the admin is actually reaching the
            # dashboard from (see dashboard.html base-url sanity check).
            config_base_url=os.environ.get("GRIDBEAR_BASE_URL", ""),
            plugins_summary={
                "channels": len(plugins["channels"]),
                "services": len(plugins["services"]),
                "mcp": len(plugins["mcp"]),
                "runners": len(plugins["runners"]),
            },
            **extra_dashboard,
        ),
    )


from core.mcp_gateway.tool_providers import discover_local_tool_providers


def _preflight_check() -> None:
    """Validate required configuration for admin UI."""
    if not os.environ.get("DATABASE_URL"):
        logger.error(
            "DATABASE_URL is not set. Ensure POSTGRES_PASSWORD is set in .env — "
            "docker-compose builds DATABASE_URL from it automatically."
        )
        raise RuntimeError("DATABASE_URL environment variable is required")

    if not os.environ.get("INTERNAL_API_SECRET"):
        logger.warning(
            "INTERNAL_API_SECRET is not set. "
            "WebChat proxy to gridbear will fail. "
            "Generate one with: openssl rand -hex 32"
        )


@app.on_event("startup")
async def startup_cleanup():
    """Cleanup expired sessions on startup and initialize OAuth2."""
    import asyncio
    import time

    from core.oauth2.models import OAuth2Database

    _preflight_check()
    app.state.start_time = time.time()

    # Check if DB is already initialized (embedded in core container)
    from core.registry import get_database

    existing_db = get_database()
    embedded = existing_db is not None

    # Initialize PostgreSQL (skip if already done by core container)
    database_url = os.environ.get("DATABASE_URL")
    try:
        if embedded:
            db_manager = existing_db
            logger.info("Admin: reusing existing PostgreSQL connection (embedded mode)")
        else:
            from core.database import DatabaseManager
            from core.registry import set_database

            db_manager = DatabaseManager(database_url)
            await db_manager.initialize()
            set_database(db_manager)
            logger.info("Admin: PostgreSQL connection pool initialized")

        if not embedded:
            # Attach DB log handler for WARNING+ persistence
            from config.logging_config import attach_db_log_handler

            attach_db_log_handler()

            # Initialize ORM: inject DB, discover models, run auto-migrations
            from core.orm import Registry as ORMRegistry

            ORMRegistry.initialize(db_manager)

        # Initialize AuthDatabase (applies migration DDL)
        init_auth_db()
        logger.info("Admin: AuthDatabase initialized")

        # Cleanup expired sessions (requires AuthDatabase)
        session_manager.cleanup_expired()

        # Warn if initial admin setup has not been completed
        from ui.auth.database import auth_db

        if auth_db.user_count() == 0:
            logger.warning(
                "SECURITY: Admin setup not completed! "
                "Visit /auth/setup to create the admin account. "
                "Until setup is completed, anyone can create the admin account."
            )

        # Re-initialize SecretsManager now that PG pool is available
        from ui.secrets_manager import reset_secrets_manager

        reset_secrets_manager()
        logger.info("Admin: SecretsManager re-initialized with PostgreSQL")

        if not embedded:
            # One-time migrations (already done by core in embedded mode)
            from core.config_migration import (
                migrate_admin_config_to_db,
                migrate_claude_settings_to_db,
                migrate_mcp_perms_to_unified_id,
                migrate_rest_api_config_to_db,
                migrate_unify_users,
                migrate_user_platforms,
            )

            await migrate_admin_config_to_db(BASE_DIR / "config" / "admin_config.json")
            await migrate_rest_api_config_to_db(BASE_DIR / "config" / "rest_api.json")
            await migrate_claude_settings_to_db(
                BASE_DIR / "config" / "claude_settings.json"
            )
            await migrate_mcp_perms_to_unified_id()
            await migrate_unify_users()
            await migrate_user_platforms()

        # Reload template loader now that DB is available (theme from SystemConfig)
        rebuild_template_loader()
        logger.info("Admin: template loader rebuilt with active theme")

        # Register plugin portal routes (requires ORM/DB for get_enabled_plugins)
        plugin_registry.register_portal_routes(app)

        # NOTE: Plugin admin routes + plugins.router are registered at module
        # level via _register_plugin_routes() — NOT here.  Starlette compiles
        # routes before startup, so include_router() in startup is a no-op.
        logger.info("Admin: plugin admin routes registered (at module level)")

        # Initialize OAuth2 database (requires PostgreSQL)
        db = OAuth2Database()
        set_oauth2_db(db)
        logger.info("OAuth2 database initialized")
    except Exception as e:
        logger.error(f"Admin: PostgreSQL initialization failed: {e}")
        raise

    # Start periodic cleanup task
    async def _oauth2_cleanup_loop():
        from core.oauth2.config import get_gateway_config

        config = get_gateway_config()
        interval = config.get("cleanup_interval_seconds", 3600)
        while True:
            await asyncio.sleep(interval)
            try:
                db.cleanup_expired()
                # Cleanup rate limiter stale entries
                from core.mcp_gateway.server import _rate_limiter

                _rate_limiter.cleanup()
                # Cleanup old tool usage metrics (90-day retention)
                from core.mcp_gateway.server import cleanup_tool_usage

                await cleanup_tool_usage()
            except Exception as e:
                logger.warning(f"OAuth2 cleanup error: {e}")

    asyncio.create_task(_oauth2_cleanup_loop())

    # Periodic notification cleanup (expired notifications)
    async def _notification_cleanup_loop():
        while True:
            await asyncio.sleep(3600)  # Every hour
            try:
                from ui.services.notifications import NotificationService

                svc = NotificationService.get()
                await svc.cleanup_expired()
            except Exception as e:
                logger.debug("Notification cleanup error: %s", e)

    asyncio.create_task(_notification_cleanup_loop())

    # Initialize MCP Gateway client manager (skipped in embedded mode or when disabled)
    if not embedded and os.getenv("MCP_GATEWAY_ENABLED", "true").lower() != "false":
        from core.mcp_gateway import server as mcp_server
        from core.mcp_gateway.client_manager import MCPClientManager

        client_manager = MCPClientManager()
        mcp_server.set_client_manager(client_manager)
        try:
            await client_manager.start()
        except Exception as e:
            logger.warning(f"MCP Gateway: client manager start failed: {e}")

        # Discover virtual tool providers from plugins
        discover_local_tool_providers(mcp_server)

        # Initialize async task manager for long-running MCP tool calls
        from core.mcp_gateway.async_tasks import AsyncTaskManager

        max_concurrent = int(os.getenv("ASYNC_TASKS_MAX_PER_AGENT", "5"))
        task_manager = AsyncTaskManager(
            notify_callback=mcp_server._send_task_notification,
            max_concurrent_per_agent=max_concurrent,
        )
        mcp_server.set_task_manager(task_manager)
        await task_manager.start()

        # Pre-connect to MCP servers in background (caches tool lists for dashboard)
        asyncio.create_task(client_manager.warm_up())
    else:
        logger.info("MCP Gateway disabled on UI container (MCP_GATEWAY_ENABLED=false)")

    # Register atexit handler to kill child processes (MCP servers) on exit.
    # This is a safety net for when shutdown_cleanup doesn't complete
    # (e.g. uvicorn --reload kills the worker before async cleanup finishes).
    atexit.register(_atexit_kill_children)


def _atexit_kill_children():
    """Kill all descendant processes on exit — prevents orphaned MCP servers."""
    pid = os.getpid()
    _kill_descendants(pid)


def _kill_descendants(pid):
    """Recursively SIGKILL all descendants of a process via /proc."""
    children = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    parts = f.read().split(")")  # comm field may contain spaces
                    fields = parts[-1].split()
                    ppid = int(fields[1])  # ppid is 4th field, index 1 after ')'
                    if ppid == pid:
                        children.append(int(entry))
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        return
    for child_pid in children:
        _kill_descendants(child_pid)
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass


# Total time budget for shutdown cleanup (seconds).
# With 10+ MCP servers, each needing up to 2s for graceful close,
# individual cleanup can take 30s+.  We cap the entire sequence.
_SHUTDOWN_TIMEOUT = 15.0


@app.on_event("shutdown")
async def shutdown_cleanup():
    """Shutdown MCP Gateway client manager and async task manager."""
    import asyncio

    try:
        await asyncio.wait_for(_do_shutdown_cleanup(), timeout=_SHUTDOWN_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "Shutdown cleanup timed out after %.0fs — force-killing children",
            _SHUTDOWN_TIMEOUT,
        )
        _atexit_kill_children()


async def _do_shutdown_cleanup():
    """Internal shutdown logic — runs under a timeout."""
    from core.mcp_gateway.server import get_client_manager, get_task_manager

    task_manager = get_task_manager()
    if task_manager:
        try:
            await task_manager.shutdown()
        except Exception as e:
            logger.warning(f"AsyncTaskManager: shutdown error: {e}")

    client_manager = get_client_manager()
    if client_manager:
        try:
            await client_manager.shutdown()
        except Exception as e:
            logger.warning(f"MCP Gateway: shutdown error: {e}")

    # Shutdown PostgreSQL pools
    from core.registry import get_database

    db_manager = get_database()
    if db_manager:
        try:
            await db_manager.shutdown()
        except Exception as e:
            logger.warning(f"Admin: PostgreSQL shutdown error: {e}")


@app.exception_handler(HTTPException)
async def custom_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(url=exc.headers.get("Location", "/auth/login"))

    # Handle CSRF validation failure with user-friendly redirect
    if exc.status_code == 403 and "CSRF" in str(exc.detail):
        # Session expired or invalid - redirect to login with message
        return RedirectResponse(
            url="/auth/login?error=session_expired",
            status_code=303,
        )

    raise exc
