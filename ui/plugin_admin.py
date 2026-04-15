"""Plugin Admin Discovery and Registration.

Discovers and registers admin routes/menus from plugins.
Each plugin can optionally have an admin/ folder with:
- routes.py: FastAPI router
- menu.json: Sidebar menu items
- templates/: Jinja2 templates
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from config.logging_config import logger

if TYPE_CHECKING:
    from core.plugin_paths import PluginPathResolver


class PluginAdminRegistry:
    """Registry for plugin admin components."""

    def __init__(
        self,
        path_resolver: "PluginPathResolver | None" = None,
        plugins_dir: Path | None = None,
    ):
        if path_resolver is not None:
            self._path_resolver = path_resolver
        elif plugins_dir is not None:
            from core.plugin_paths import PluginPathResolver

            self._path_resolver = PluginPathResolver([plugins_dir])
        else:
            raise ValueError("Either path_resolver or plugins_dir must be provided")
        self.menu_items: list[dict] = []
        self.plugin_configs: dict[str, dict] = {}

    @property
    def plugins_dir(self) -> Path:
        """Primary plugins directory (backward compat)."""
        return self._path_resolver.dirs[0]

    def get_enabled_plugins(self) -> list[str]:
        """Get list of enabled plugin names from the DB registry."""
        try:
            from core.plugin_registry.models import PluginRegistryEntry

            entries = PluginRegistryEntry.search_sync(
                [("state", "=", "installed"), ("enabled", "=", True)],
                order="name",
            )
            return [e["name"] for e in entries]
        except Exception as exc:
            logger.debug("get_enabled_plugins from DB failed: %s", exc)
        return []

    def discover_plugin_menus(self) -> list[dict]:
        """Discover menu items from all enabled plugins."""
        menus = []
        enabled = self.get_enabled_plugins()

        for plugin_name in enabled:
            plugin_path = self._path_resolver.resolve(plugin_name)
            if plugin_path is None:
                continue
            menu_path = plugin_path / "admin" / "menu.json"

            if menu_path.exists():
                try:
                    with open(menu_path) as f:
                        menu_data = json.load(f)
                        # Add plugin name to each menu item
                        for item in menu_data.get("items", []):
                            item["plugin"] = plugin_name
                            menus.append(item)
                except Exception as e:
                    logger.warning(f"Failed to load menu for {plugin_name}: {e}")

        # Sort by priority (lower = higher in menu)
        menus.sort(key=lambda x: x.get("priority", 100))
        self.menu_items = menus
        return menus

    def discover_plugin_configs(self) -> dict[str, dict]:
        """Discover configuration schemas from all plugins."""
        configs = {}

        for name, manifest in self._path_resolver.discover_all().items():
            try:
                configs[name] = {
                    "name": manifest.get("name", name),
                    "type": manifest.get("type", "unknown"),
                    "description": manifest.get("description", ""),
                    "version": manifest.get("version", "1.0.0"),
                    "provides": manifest.get("provides"),
                    "config_schema": manifest.get("config_schema", {}),
                }
            except Exception as e:
                logger.warning(f"Failed to process manifest for {name}: {e}")

        self.plugin_configs = configs
        return configs

    @staticmethod
    def _import_plugin_module(plugin_name: str, plugin_path: Path, routes_path: Path):
        """Import a plugin sub-module with proper package context.

        Uses importlib.import_module with the dotted path derived from the
        filesystem layout so that relative imports inside plugin code work
        correctly (e.g. ``from ..models import Foo``).
        """
        # Derive dotted module path from filesystem: plugins/<name>/portal/routes.py
        # → plugins.<name>.portal.routes
        try:
            rel = routes_path.relative_to(plugin_path.parent.parent)
        except ValueError:
            rel = routes_path.relative_to(Path.cwd())
        module_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

        # Ensure parent packages are in sys.modules so relative imports work
        parts = module_name.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent not in sys.modules:
                parent_path = Path.cwd() / Path(*parts[:i])
                init_file = parent_path / "__init__.py"
                if init_file.exists():
                    spec = importlib.util.spec_from_file_location(
                        parent,
                        str(init_file),
                        submodule_search_locations=[str(parent_path)],
                    )
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[parent] = mod
                    spec.loader.exec_module(mod)
                else:
                    # Namespace package — create a minimal entry
                    mod = importlib.util.module_from_spec(
                        importlib.machinery.ModuleSpec(
                            parent,
                            None,
                            is_package=True,
                        )
                    )
                    mod.__path__ = [str(parent_path)]
                    sys.modules[parent] = mod

        return importlib.import_module(module_name)

    def register_portal_routes(self, app: FastAPI) -> None:
        """Register user-facing portal routes from enabled plugins.

        Discovers plugins/{name}/portal/routes.py and includes the router.
        Portal routers MUST define their own prefix (e.g. '/me/workflows').
        """
        enabled = self.get_enabled_plugins()

        for plugin_name in enabled:
            plugin_path = self._path_resolver.resolve(plugin_name)
            if plugin_path is None:
                continue
            routes_path = plugin_path / "portal" / "routes.py"

            if routes_path.exists():
                try:
                    module = self._import_plugin_module(
                        plugin_name, plugin_path, routes_path
                    )

                    if hasattr(module, "router"):
                        app.include_router(
                            module.router,
                            tags=[f"portal-{plugin_name}"],
                        )
                        logger.info(
                            f"Registered portal routes for plugin: {plugin_name}"
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to register portal routes for {plugin_name}: {e}"
                    )

    def register_plugin_routes(self, app: FastAPI) -> None:
        """Register admin routes from all enabled plugins.

        If the plugin router already defines a prefix (e.g. '/plugins/ms365'),
        it is mounted as-is. Otherwise, a default '/plugin/{name}' prefix is added.
        """
        enabled = self.get_enabled_plugins()

        for plugin_name in enabled:
            plugin_path = self._path_resolver.resolve(plugin_name)
            if plugin_path is None:
                continue
            routes_path = plugin_path / "admin" / "routes.py"

            if routes_path.exists():
                try:
                    module = self._import_plugin_module(
                        plugin_name, plugin_path, routes_path
                    )

                    if hasattr(module, "router"):
                        if module.router.prefix:
                            app.include_router(
                                module.router,
                                tags=[f"plugin-{plugin_name}"],
                            )
                        else:
                            app.include_router(
                                module.router,
                                prefix=f"/plugin/{plugin_name}",
                                tags=[f"plugin-{plugin_name}"],
                            )
                        logger.info(
                            f"Registered admin routes for plugin: {plugin_name}"
                        )
                except Exception as e:
                    logger.error(f"Failed to register routes for {plugin_name}: {e}")

    def register_plugin_routes_sync(self, app: "FastAPI") -> None:
        """Register plugin admin routes at module level (before startup).

        Uses a direct psycopg query to discover enabled plugins because
        the ORM is not yet initialized when this runs.
        """
        enabled = self._get_enabled_plugins_raw()
        if not enabled:
            logger.debug("No enabled plugins found (early route registration)")
            return

        for plugin_name in enabled:
            plugin_path = self._path_resolver.resolve(plugin_name)
            if plugin_path is None:
                continue
            routes_path = plugin_path / "admin" / "routes.py"

            if routes_path.exists():
                try:
                    module = self._import_plugin_module(
                        plugin_name, plugin_path, routes_path
                    )
                    if hasattr(module, "router"):
                        if module.router.prefix:
                            app.include_router(
                                module.router,
                                tags=[f"plugin-{plugin_name}"],
                            )
                        else:
                            app.include_router(
                                module.router,
                                prefix=f"/plugin/{plugin_name}",
                                tags=[f"plugin-{plugin_name}"],
                            )
                        logger.info(
                            "Registered admin routes for plugin: %s", plugin_name
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to register routes for %s: %s", plugin_name, exc
                    )

            # Also mount api/routes.py if plugin declares public_api in manifest
            api_routes_path = plugin_path / "api" / "routes.py"
            if api_routes_path.exists():
                manifest_path = plugin_path / "manifest.json"
                if manifest_path.exists():
                    import json

                    try:
                        manifest = json.loads(manifest_path.read_text())
                    except Exception:
                        manifest = {}
                    if not manifest.get("public_api"):
                        continue
                try:
                    module = self._import_plugin_module(
                        plugin_name, plugin_path, api_routes_path
                    )
                    if hasattr(module, "router"):
                        prefix = manifest.get(
                            "public_api_prefix", f"/api/{plugin_name}"
                        )
                        app.include_router(
                            module.router,
                            prefix=prefix,
                            tags=[f"api-{plugin_name}"],
                        )
                        logger.info(
                            "Registered public API routes for plugin: %s at %s",
                            plugin_name,
                            prefix,
                        )
                except Exception as exc:
                    logger.error(
                        "Failed to register API routes for %s: %s",
                        plugin_name,
                        exc,
                    )

    @staticmethod
    def _get_enabled_plugins_raw() -> list[str]:
        """Query enabled plugins via psycopg (no ORM needed)."""
        import os

        try:
            import psycopg

            dsn = os.environ.get("DATABASE_URL", "")
            if not dsn:
                return []
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name FROM app.plugin_registry "
                        "WHERE state = 'installed' AND enabled = true "
                        "ORDER BY name"
                    )
                    return [row[0] for row in cur.fetchall()]
        except Exception as exc:
            logger.debug("_get_enabled_plugins_raw failed: %s", exc)
            return []

    def get_plugins_by_type(self) -> dict[str, list[str]]:
        """Get enabled plugins grouped by type."""
        result = {
            "runner": [],
            "channel": [],
            "service": [],
            "mcp": [],
        }

        enabled = self.get_enabled_plugins()
        configs = self.discover_plugin_configs()

        for plugin_name in enabled:
            if plugin_name in configs:
                plugin_type = configs[plugin_name].get("type", "unknown")
                if plugin_type in result:
                    result[plugin_type].append(plugin_name)

        return result

    def get_plugin_config(self, plugin_name: str) -> dict:
        """Get current configuration for a plugin from DB."""
        from ui.plugin_helpers import load_plugin_config

        return load_plugin_config(plugin_name)

    def save_plugin_config(self, plugin_name: str, config: dict) -> None:
        """Save configuration for a plugin to DB."""
        from ui.plugin_helpers import save_plugin_config

        save_plugin_config(plugin_name, config)

    def toggle_plugin(self, plugin_name: str) -> bool:
        """Toggle plugin enabled state via DB registry. Returns new state."""
        try:
            from core.plugin_registry.models import PluginRegistryEntry

            entry = PluginRegistryEntry.get_sync(name=plugin_name)
            if not entry:
                logger.warning("toggle_plugin: '%s' not in registry", plugin_name)
                return False
            new_state = not entry.get("enabled", False)
            PluginRegistryEntry.write_sync(entry["id"], enabled=new_state)
            return new_state
        except Exception as exc:
            logger.error("toggle_plugin(%s) failed: %s", plugin_name, exc)
            return False
