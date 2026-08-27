"""Tool management routes for Admin UI."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from core.api_schemas import ApiResponse, api_error, api_ok
from ui.routes.auth import require_login

router = APIRouter()

_templates = None


def get_templates():
    """Get templates instance from app module."""
    global _templates
    if _templates is None:
        from ui.app import templates

        _templates = templates
    return _templates


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context."""
    from ui.app import get_enabled_plugins_by_type

    plugins = get_enabled_plugins_by_type()
    plugin_menus = getattr(request.state, "plugin_menus", [])

    return {
        "request": request,
        "enabled_channels": plugins.get("channels", []),
        "enabled_services": plugins.get("services", []),
        "enabled_mcp": plugins.get("mcp", []),
        "enabled_runners": plugins.get("runners", []),
        "plugin_menus": plugin_menus,
        **kwargs,
    }


async def _get_dashboard_data() -> dict:
    """Gather all tool dashboard statistics."""
    from core.mcp_gateway.server import get_client_manager
    from core.registry import get_database

    db = get_database()
    data = {
        "total_calls": 0,
        "success_rate": 0,
        "avg_duration": 0,
        "top_tools": [],
        "agent_stats": [],
        "mcp_servers": [],
        "unused_tools": [],
    }

    # --- tool_usage stats (7 days) ---
    if db:
        try:
            row = await db.fetch_one(
                "SELECT COUNT(*) AS cnt, "
                "COALESCE(AVG(success::int) * 100, 0) AS rate, "
                "COALESCE(AVG(duration_ms), 0) AS avg_ms "
                "FROM public.tool_usage "
                "WHERE called_at > NOW() - INTERVAL '7 days'"
            )
            if row:
                data["total_calls"] = row["cnt"]
                data["success_rate"] = round(row["rate"], 1)
                data["avg_duration"] = round(row["avg_ms"])
        except Exception:
            pass

        # Top 20 tools (7 days)
        try:
            rows = await db.fetch_all(
                "SELECT tool_name, COUNT(*) AS cnt, "
                "ROUND(AVG(success::int) * 100) AS rate, "
                "ROUND(AVG(duration_ms)) AS avg_ms "
                "FROM public.tool_usage "
                "WHERE called_at > NOW() - INTERVAL '7 days' "
                "GROUP BY tool_name "
                "ORDER BY cnt DESC LIMIT 20"
            )
            data["top_tools"] = [
                {
                    "name": r["tool_name"],
                    "server": r["tool_name"].split("__", 1)[0]
                    if "__" in r["tool_name"]
                    else "builtin",
                    "calls": r["cnt"],
                    "success_rate": r["rate"],
                    "avg_ms": r["avg_ms"],
                }
                for r in rows
            ]
        except Exception:
            pass

        # Per-agent stats (7 days)
        try:
            rows = await db.fetch_all(
                "SELECT agent_name, COUNT(*) AS cnt, "
                "COUNT(DISTINCT tool_name) AS unique_tools, "
                "ROUND(AVG(duration_ms)) AS avg_ms "
                "FROM public.tool_usage "
                "WHERE called_at > NOW() - INTERVAL '7 days' "
                "GROUP BY agent_name ORDER BY cnt DESC"
            )
            data["agent_stats"] = [
                {
                    "agent": r["agent_name"],
                    "calls": r["cnt"],
                    "unique_tools": r["unique_tools"],
                    "avg_ms": r["avg_ms"],
                }
                for r in rows
            ]
        except Exception:
            pass

        # Collect used tool names (30 days) for unused detection
        used_names: set[str] = set()
        try:
            rows = await db.fetch_all(
                "SELECT DISTINCT tool_name FROM public.tool_usage "
                "WHERE called_at > NOW() - INTERVAL '30 days'"
            )
            used_names = {r["tool_name"] for r in rows}
        except Exception:
            pass

    # --- MCP server info ---
    client_manager = get_client_manager()
    all_tool_names: set[str] = set()
    if client_manager:
        for name, info in client_manager._known_servers.items():
            conn = client_manager._connections.get(name)
            tool_count = len(conn.tools) if conn and conn.tools else 0
            for t in conn.tools if conn and conn.tools else []:
                all_tool_names.add(t.get("name", ""))

            if conn:
                if conn.connected:
                    status = "connected"
                elif conn.failed:
                    status = "failed"
                else:
                    status = "disconnected"
            else:
                status = "pending"

            data["mcp_servers"].append(
                {
                    "name": name,
                    "category": info.category,
                    "transport": info.transport,
                    "tools": tool_count,
                    "status": status,
                }
            )

        # Sort servers by name
        data["mcp_servers"].sort(key=lambda s: s["name"])

        # Unused tools (in MCP but not called in 30 days)
        if db and all_tool_names:
            unused = sorted(all_tool_names - used_names)
            data["unused_tools"] = unused

    # Load agent configs for budget/tool_loading info
    agent_map: dict[str, dict] = {}
    try:
        from ui.routes.agents import list_agents as _list_agents

        for a in _list_agents():
            from ui.routes.agents import load_agent

            cfg = load_agent(a["id"])
            if cfg:
                agent_map[a["id"]] = {
                    "max_tools": cfg.get("max_tools"),
                    "tool_loading": cfg.get("tool_loading", "full"),
                }
    except Exception:
        pass
    data["agent_map"] = agent_map

    return data


@router.get("/", response_class=HTMLResponse)
async def tools_dashboard(request: Request, _: dict = Depends(require_login)):
    """Tool management dashboard."""
    data = await _get_dashboard_data()

    return get_templates().TemplateResponse(
        request,
        "tools/dashboard.html",
        get_template_context(request, **data),
    )


@router.get("/{agent_id}/tools", response_class=HTMLResponse)
async def agent_tools_page(
    request: Request, agent_id: str, _: dict = Depends(require_login)
):
    """Per-agent tool enable/disable page."""
    from core.mcp_gateway.server import get_client_manager
    from ui.auth.database import get_auth_db
    from ui.routes.agents import load_agent

    agent = load_agent(agent_id)
    if not agent:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Agent not found")

    tool_groups: dict[str, list[dict]] = {}
    client_manager = get_client_manager()
    if client_manager:
        try:
            from core.mcp_gateway.client_manager import NS_SEP, _sanitize_name

            prefs = get_auth_db().get_agent_disabled_tools(agent_id)
            mcp_perms = agent.get("mcp_permissions", [])

            # Use cached tools from already-connected servers
            # (populated after first tools/list from runner)
            for server_name, conn in client_manager._connections.items():
                if not conn.tools:
                    continue
                # Filter by agent's MCP permissions if set
                if mcp_perms:
                    from core.permissions.mcp_resolver import matches_permission

                    if not matches_permission(server_name, mcp_perms):
                        continue

                sanitized = _sanitize_name(server_name)
                for tool in conn.tools:
                    full_name = f"{sanitized}{NS_SEP}{tool['name']}"
                    if len(full_name) > 64:
                        full_name = full_name[:64]

                    if sanitized not in tool_groups:
                        tool_groups[sanitized] = []

                    tool_groups[sanitized].append(
                        {
                            "full_name": full_name,
                            "short_name": tool["name"],
                            "description": tool.get("description", ""),
                            "enabled": full_name not in prefs,
                        }
                    )
        except Exception:
            pass

    return get_templates().TemplateResponse(
        request,
        "tools/agent_tools.html",
        get_template_context(
            request,
            agent=agent,
            agent_id=agent_id,
            tool_groups=tool_groups,
        ),
    )


@router.post(
    "/{agent_id}/tools/toggle",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def agent_tool_toggle(
    request: Request, agent_id: str, _: dict = Depends(require_login)
):
    """Toggle a tool preference for an agent."""
    try:
        body = await request.json()
    except Exception:
        return api_error(400, "Invalid JSON", "validation_error")

    tool_name = body.get("tool_name", "")
    enabled = body.get("enabled", True)

    if not tool_name:
        return api_error(400, "Missing tool_name", "validation_error")

    from ui.auth.database import get_auth_db

    get_auth_db().set_agent_tool_pref(agent_id, tool_name, enabled)

    return api_ok()
