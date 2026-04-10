"""Discover and register LocalToolProvider instances from plugins."""

import importlib.util
import logging

logger = logging.getLogger(__name__)


def discover_local_tool_providers(mcp_server) -> None:
    """Discover and register LocalToolProvider instances from plugins.

    Scans enabled plugins for a 'virtual_tools' entry in manifest.json.
    Uses the path resolver to find plugins across multiple directories.
    """
    from core.plugin_registry.models import PluginRegistryEntry
    from core.registry import get_path_resolver

    resolver = get_path_resolver()

    rows = PluginRegistryEntry.search_sync([("enabled", "=", True)])
    enabled = [r["name"] for r in rows]
    if not enabled:
        return

    all_manifests = resolver.discover_all() if resolver else {}
    providers = []

    for plugin_name in enabled:
        manifest = all_manifests.get(plugin_name)
        if manifest is None:
            continue

        vt_file = manifest.get("virtual_tools")
        if not vt_file:
            continue

        plugin_dir = resolver.resolve(plugin_name) if resolver else None
        if plugin_dir is None:
            continue

        vt_path = plugin_dir / vt_file
        if not vt_path.exists():
            logger.warning(f"Virtual tools file not found: {vt_path}")
            continue

        try:
            safe_name = plugin_name.replace("-", "_")
            spec = importlib.util.spec_from_file_location(
                f"{safe_name}_virtual_tools",
                vt_path,
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find VirtualToolProvider subclass in module
            from core.interfaces.local_tools import LocalToolProvider

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, LocalToolProvider)
                    and attr is not LocalToolProvider
                ):
                    instance = attr()
                    providers.append(instance)
                    logger.info(
                        f"Registered virtual tool provider: {instance.get_server_name()} "
                        f"({len(instance.get_tools())} tools) from {plugin_name}"
                    )
                    break
        except Exception as e:
            logger.error(f"Failed to load virtual tools from {plugin_name}: {e}")

    mcp_server.set_local_tool_providers(providers)
