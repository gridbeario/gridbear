"""Dynamic MCP server discovery for the Admin UI.

Mirrors ``ui/utils/channels.py``: runtime ``PluginManager`` first, fallback to
plugin registry DB + static manifest discovery. Needed because the UI container
does not call ``set_plugin_manager()`` — only the bot container does — so
``core.registry.get_available_mcp_servers()`` always returns ``[]`` under the
admin app.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_available_mcp_servers() -> list[str]:
    """Return all enabled MCP server names visible to the admin UI.

    Runtime ``PluginManager`` is queried first; if unavailable (UI container
    case), fall back to enabled plugins in the DB registry, resolving each
    plugin's static server name(s) from its manifest.
    """
    try:
        from core.registry import get_plugin_manager

        pm = get_plugin_manager()
        if pm is not None:
            names = pm.get_all_mcp_server_names()
            if names:
                return sorted(names)
    except Exception as exc:
        logger.debug("runtime plugin_manager lookup failed: %s", exc)

    return sorted(_static_server_names())


def _static_server_names() -> list[str]:
    """Resolve MCP server names from DB-enabled plugins + their manifests.

    Multi-instance providers (``mcp_naming == per_account`` / ``per_tenant``)
    require runtime expansion and are skipped here — their instances only exist
    once the bot container has started. The fallback therefore covers
    single-server plugins, which is the common case for the first-boot form.
    """
    from core.registry import get_path_resolver
    from ui.plugin_helpers import get_enabled_plugins

    resolver = get_path_resolver()
    manifests = resolver.discover_all() if resolver else {}
    enabled = get_enabled_plugins()

    names: list[str] = []
    for plugin_name in enabled:
        manifest = manifests.get(plugin_name)
        if not manifest:
            continue
        # Two surfaces are exposed through the gateway: dedicated `type: mcp`
        # plugins and `type: service` plugins that declare `virtual_tools`
        # in their manifest (see core/mcp_gateway/tool_providers.py). Both
        # become entries in the gateway's tool list and both must be
        # selectable from the agent admin form, otherwise virtual-tool
        # providers are unreachable from the UI (gridbeario/gridbear#152).
        if manifest.get("type") != "mcp" and not manifest.get("virtual_tools"):
            continue
        if manifest.get("mcp_naming") in {"per_account", "per_tenant"}:
            continue
        names.append(manifest.get("name") or plugin_name)
    return names
