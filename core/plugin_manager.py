"""Plugin Manager.

Discovers, loads and manages all GridBear plugins.
Plugins are in flat structure: plugins/<name>/ with manifest.json defining type.

Supports plugin extension via:
- `depends`: List of plugins that must be loaded first
- `provides`: Register under a different name (for extension/replacement)

Example extension:
    # plugins/custom-memory/manifest.json
    {
        "name": "custom-memory",
        "type": "service",
        "provides": "memory",
        "depends": ["memory"],
        ...
    }

    # plugins/custom-memory/service.py
    from gridbear.plugins.memory import MemoryService

    class CustomMemoryService(MemoryService):
        async def add_episodic_memory(self, ...):
            await super().add_episodic_memory(...)
            # custom behavior
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config.logging_config import logger
from core.hooks import HookName, hook_manager
from core.interfaces.channel import BaseChannel
from core.interfaces.mcp_provider import BaseMCPProvider
from core.interfaces.runner import BaseRunner
from core.interfaces.service import BaseService
from core.interfaces.theme import BaseTheme

if TYPE_CHECKING:
    from core.plugin_paths import PluginPathResolver
    from core.plugin_registry import PluginRegistry


class PluginManager:
    """Manages plugin discovery, loading and lifecycle.

    Supports Odoo-style plugin extension:
    - Plugins declare dependencies via `depends` in manifest
    - Plugins can provide/replace others via `provides` in manifest
    - Load order is determined by dependency resolution (topological sort)
    - Last loaded wins when multiple plugins provide the same service
    """

    def __init__(
        self,
        path_resolver: "PluginPathResolver | None" = None,
        plugins_dir: Path | None = None,
        config_path: Path | None = None,
    ):
        """Initialize plugin manager.

        Args:
            path_resolver: PluginPathResolver for multi-directory support.
                          If None, falls back to plugins_dir (backward compat).
            plugins_dir: Legacy single-directory path (deprecated, use path_resolver).
            config_path: Deprecated — only used by migration code in registry.
        """
        if path_resolver is not None:
            self._path_resolver = path_resolver
        elif plugins_dir is not None:
            from core.plugin_paths import PluginPathResolver

            self._path_resolver = PluginPathResolver([plugins_dir])
        else:
            raise ValueError("Either path_resolver or plugins_dir must be provided")
        self.config_path = config_path

        self.runners: dict[str, BaseRunner] = {}
        self.channels: dict[str, BaseChannel] = {}
        self.services: dict[str, BaseService] = {}
        self.mcp_providers: dict[str, BaseMCPProvider] = {}
        self.themes: dict[str, BaseTheme] = {}

        self._manifests: dict[str, dict] = {}
        self._modules: dict[str, Any] = {}  # Store loaded modules for hook discovery
        self._plugin_classes: dict[
            str, type
        ] = {}  # Plugin classes for per-agent instantiation
        self._provides_map: dict[str, str] = {}  # maps "provides" name -> plugin name
        self._context_injections: dict[str, str] = {}  # plugin name -> context string

        self.hooks = hook_manager
        self._registry: "PluginRegistry | None" = None

    @property
    def registry(self) -> "PluginRegistry | None":
        """PostgreSQL-backed plugin state registry (available after first load)."""
        return self._registry

    @property
    def plugins_dir(self) -> Path:
        """Primary plugins directory (first in resolver list, backward compat)."""
        return self._path_resolver.dirs[0]

    async def load_all(self, exclude_types: list[str] | None = None) -> None:
        """Load all enabled plugins in dependency order.

        Plugins are loaded based on:
        1. Dependencies (topological sort) - plugins with depends load after their dependencies
        2. Type order - services first, then runners, channels, mcp

        Args:
            exclude_types: List of plugin types to skip during loading (e.g., ["channel"]).
                          Modules are still registered for import but not instantiated.
        """
        config = self._load_config()
        self._plugins_config = config
        exclude_types = exclude_types or []

        # Discover all plugins and their manifests
        plugin_info = self._discover_plugins()

        # Initialize registry and sync with disk
        from core.plugin_registry import PluginRegistry

        self._registry = PluginRegistry()
        if self.config_path:
            await self._registry.migrate_from_config(self.config_path, plugin_info)
            await self._registry.migrate_config_from_file(self.config_path)
        await self._registry.sync_with_disk(plugin_info)
        enabled = await self._registry.get_enabled_plugins()

        if not enabled:
            logger.warning("No plugins enabled")
            return

        # Filter to only enabled plugins that were discovered on disk
        enabled_plugins = {}
        for name in enabled:
            if name not in plugin_info:
                logger.warning(f"Plugin '{name}' enabled but not found on disk")
                continue
            enabled_plugins[name] = plugin_info[name]

        # Resolve dependencies and get load order
        try:
            load_order = self._resolve_dependencies(enabled_plugins)
        except ValueError as e:
            logger.error(f"Dependency resolution failed: {e}")
            return

        # Group by type while preserving dependency order
        type_order = {"theme": -1, "service": 0, "runner": 1, "channel": 2, "mcp": 3}

        def sort_key(name: str) -> tuple:
            manifest = enabled_plugins[name]
            type_idx = type_order.get(manifest.get("type", ""), 99)
            dep_idx = load_order.index(name)
            return (type_idx, dep_idx)

        sorted_plugins = sorted(enabled_plugins.keys(), key=sort_key)

        # Load plugins in order
        for name in sorted_plugins:
            manifest = enabled_plugins[name]
            plugin_config = config.get(name, {})
            plugin_type = manifest.get("type", "")
            skip_instantiate = plugin_type in exclude_types
            await self._load_plugin(name, plugin_config, manifest, skip_instantiate)

        # Count registered hooks
        registered_hooks = self.hooks.list_hooks()
        hook_count = sum(len(hooks) for hooks in registered_hooks.values())

        theme_info = f", {len(self.themes)} themes" if self.themes else ""
        logger.info(
            f"Loaded plugins: {len(self.services)} services, "
            f"{len(self.runners)} runners, {len(self.channels)} channels, "
            f"{len(self.mcp_providers)} MCP providers, {hook_count} hooks"
            f"{theme_info}"
        )

        # Post-load validation: check declared dependencies are satisfied
        self._validate_dependencies()

        # Log hook registration summary (§3.4 — detect missing handlers)
        self._log_hook_summary()

        # Seed context skills from plugin .md files into DB
        await self._seed_context_skills()

        # Execute startup hook
        await self.hooks.execute(HookName.ON_STARTUP, {"plugin_manager": self})

    def _parse_dependencies(self, manifest: dict) -> tuple[list[str], list[str]]:
        """Parse dependencies from manifest, supporting both legacy and new format.

        Legacy format: "dependencies": ["sessions", "attachments"]  -> all required
        New format: "dependencies": {"required": [...], "optional": [...]}

        Returns:
            Tuple of (required_deps, optional_deps)
        """
        deps_raw = manifest.get("dependencies") or manifest.get("depends") or []
        if isinstance(deps_raw, list):
            return deps_raw, []
        elif isinstance(deps_raw, dict):
            return deps_raw.get("required", []), deps_raw.get("optional", [])
        return [], []

    def _validate_dependencies(self) -> None:
        """Validate that all required dependencies are satisfied after loading.

        Called after load_all() completes. Logs errors for missing required
        dependencies and info for missing optional dependencies.
        """
        # Build set of all available plugin names (loaded plugins + provides names)
        available = set()
        available.update(self.services.keys())
        available.update(self.runners.keys())
        available.update(self.channels.keys())
        available.update(self.mcp_providers.keys())
        available.update(self.themes.keys())
        available.update(self._plugin_classes.keys())

        known_plugins = self._all_known_plugin_names()

        for name, manifest in self._manifests.items():
            required_deps, optional_deps = self._parse_dependencies(manifest)

            for dep in required_deps:
                if dep not in available and dep in known_plugins:
                    logger.error(
                        f"Plugin '{name}': required dependency '{dep}' is not loaded"
                    )

            missing_optional = [
                dep
                for dep in optional_deps
                if dep not in available and dep in known_plugins
            ]
            if missing_optional:
                logger.info(
                    f"Plugin '{name}': optional dependencies not available: "
                    f"{missing_optional}"
                )

    def _all_known_plugin_names(self) -> set[str]:
        """Get all known plugin names from disk discovery.

        Used to distinguish real plugin dependencies from non-plugin strings
        (e.g. ms365 declares Python packages as dependencies).
        """
        try:
            discovered = self._discover_plugins()
            names = set(discovered.keys())
            # Also include provides names
            for manifest in discovered.values():
                provides = manifest.get("provides")
                if provides:
                    names.add(provides)
            return names
        except Exception:
            return set()

    def _log_hook_summary(self) -> None:
        """Log which hooks have registered handlers after all plugins loaded."""
        registered = self.hooks.list_hooks()
        if registered:
            for hook_name, handlers in registered.items():
                logger.debug(f"Hook '{hook_name}': {len(handlers)} handler(s)")
        else:
            logger.info("No hook handlers registered by any plugin")

    def get_dependents(self, plugin_name: str) -> dict[str, list[str]]:
        """Find plugins that depend on a given plugin.

        Args:
            plugin_name: Plugin name to check dependents for

        Returns:
            Dict with 'required_by' and 'optional_for' lists of plugin names
        """
        required_by = []
        optional_for = []

        for name, manifest in self._manifests.items():
            if name == plugin_name:
                continue
            required_deps, optional_deps = self._parse_dependencies(manifest)
            if plugin_name in required_deps:
                required_by.append(name)
            elif plugin_name in optional_deps:
                optional_for.append(name)

        return {"required_by": required_by, "optional_for": optional_for}

    def _resolve_dependencies(self, plugins: dict[str, dict]) -> list[str]:
        """Resolve plugin dependencies using topological sort.

        Args:
            plugins: Dict mapping plugin name to manifest

        Returns:
            List of plugin names in load order (dependencies first)

        Raises:
            ValueError: If circular dependency detected
        """
        # Build dependency graph
        # Also consider "provides" - if plugin B provides "memory" and
        # plugin C depends on "memory", C depends on B
        provides_map = {}
        for name, manifest in plugins.items():
            provides = manifest.get("provides", name)
            provides_map[provides] = name

        # Build adjacency list (plugin -> plugins it depends on)
        dependencies: dict[str, set[str]] = {name: set() for name in plugins}

        for name, manifest in plugins.items():
            required_deps, optional_deps = self._parse_dependencies(manifest)
            all_deps = required_deps + optional_deps
            for dep in all_deps:
                # Resolve "provides" names to actual plugin names
                actual_dep = provides_map.get(dep, dep)
                if actual_dep in plugins:
                    dependencies[name].add(actual_dep)

        # Topological sort using Kahn's algorithm
        # Count incoming edges
        in_degree = {name: 0 for name in plugins}
        for name, deps in dependencies.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[name] += 1

        # Start with nodes that have no dependencies
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []

        while queue:
            # Sort queue for deterministic order
            queue.sort()
            node = queue.pop(0)
            result.append(node)

            # Reduce in-degree for nodes that depend on this one
            for name, deps in dependencies.items():
                if node in deps:
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        queue.append(name)

        if len(result) != len(plugins):
            # Circular dependency detected
            remaining = set(plugins.keys()) - set(result)
            raise ValueError(f"Circular dependency detected involving: {remaining}")

        return result

    def _discover_plugins(self) -> dict[str, dict]:
        """Discover all available plugins by scanning all plugin directories.

        Returns:
            Dict mapping plugin name to manifest
        """
        return self._path_resolver.discover_all()

    async def _load_plugin(
        self, name: str, config: dict, manifest: dict, skip_instantiate: bool = False
    ) -> None:
        """Load a single plugin.

        Args:
            name: Plugin name (directory name)
            config: Plugin-specific configuration
            manifest: Plugin manifest
            skip_instantiate: If True, only register the module but don't create instance.
                             Used for channels in multi-agent mode where AgentManager
                             creates channel instances.

        The plugin can declare:
        - `provides`: Register under this name instead of plugin name (for extension)
        - `depends`: List of plugins that must be loaded first
        - `instantiation`: "shared" (default) or "per-agent"
        """
        plugin_path = self._path_resolver.resolve(name)
        if plugin_path is None:
            logger.error(f"Plugin {name} not found in any plugin directory")
            try:
                import asyncio

                from core.notifications_client import send_notification

                asyncio.ensure_future(
                    send_notification(
                        category="plugin_error",
                        severity="error",
                        title=f"Plugin failed: {name}",
                        message=f"Plugin {name} not found in any plugin directory",
                        source=name,
                    )
                )
            except Exception:
                pass
            return
        plugin_type = manifest.get("type")
        provides = manifest.get("provides", name)  # Default: provides itself
        instantiation = manifest.get("instantiation", "shared")  # Default: shared

        self._manifests[name] = manifest

        # Dynamic import
        entry_point = plugin_path / manifest["entry_point"]
        if not entry_point.exists():
            logger.error(f"Plugin {name} entry point not found: {entry_point}")
            try:
                import asyncio

                from core.notifications_client import send_notification

                asyncio.ensure_future(
                    send_notification(
                        category="plugin_error",
                        severity="error",
                        title=f"Plugin failed: {name}",
                        message=f"Entry point not found: {entry_point}",
                        source=name,
                    )
                )
            except Exception:
                pass
            return

        # Create module name for import system
        module_name = f"gridbear.plugins.{name}"

        # Ensure parent namespace packages exist in sys.modules
        # This enables: from gridbear.plugins.memory import MemoryService
        import types

        if "gridbear" not in sys.modules:
            gridbear_pkg = types.ModuleType("gridbear")
            gridbear_pkg.__path__ = []
            gridbear_pkg.__package__ = "gridbear"
            sys.modules["gridbear"] = gridbear_pkg
        if "gridbear.plugins" not in sys.modules:
            plugins_pkg = types.ModuleType("gridbear.plugins")
            plugins_pkg.__path__ = [str(d) for d in self._path_resolver.dirs]
            plugins_pkg.__package__ = "gridbear.plugins"
            sys.modules["gridbear.plugins"] = plugins_pkg
        else:
            # Ensure all resolver dirs are in __path__
            existing = sys.modules["gridbear.plugins"]
            for d in self._path_resolver.dirs:
                if str(d) not in existing.__path__:
                    existing.__path__.append(str(d))

        spec = importlib.util.spec_from_file_location(
            module_name,
            entry_point,
            submodule_search_locations=[str(plugin_path)],
        )
        if spec is None or spec.loader is None:
            logger.error(f"Failed to load plugin spec: {name}")
            try:
                import asyncio

                from core.notifications_client import send_notification

                asyncio.ensure_future(
                    send_notification(
                        category="plugin_error",
                        severity="error",
                        title=f"Plugin failed: {name}",
                        message=f"Failed to load plugin spec: {name}",
                        source=name,
                    )
                )
            except Exception:
                pass
            return

        module = importlib.util.module_from_spec(spec)
        # Set __path__ for package-like behavior (enables relative imports)
        module.__path__ = [str(plugin_path)]
        module.__package__ = module_name

        # Register in sys.modules BEFORE executing so other plugins can import
        # This enables: from gridbear.plugins.memory import MemoryService
        sys.modules[module_name] = module

        # Also register shorter alias for convenience
        # This enables: from plugins.memory import MemoryService
        short_module_name = f"plugins.{name}"
        sys.modules[short_module_name] = module

        spec.loader.exec_module(module)

        # Store module for hook discovery
        self._modules[name] = module

        # Get the plugin class
        class_name = manifest["class_name"]
        if not hasattr(module, class_name):
            logger.error(f"Plugin {name} missing class: {class_name}")
            try:
                import asyncio

                from core.notifications_client import send_notification

                asyncio.ensure_future(
                    send_notification(
                        category="plugin_error",
                        severity="error",
                        title=f"Plugin failed: {name}",
                        message=f"Plugin {name} missing class: {class_name}",
                        source=name,
                    )
                )
            except Exception:
                pass
            return

        cls = getattr(module, class_name)

        # For per-agent plugins, only register the class without instantiating
        if instantiation == "per-agent":
            self._plugin_classes[provides] = cls
            if name != provides:
                self._plugin_classes[name] = cls
            self._provides_map[provides] = name
            logger.info(
                f"Registered plugin class: {name} (per-agent, provides {provides}) "
                f"v{manifest.get('version', '?')}"
            )
            return

        # If skip_instantiate, just register module and return (for multi-agent mode)
        if skip_instantiate:
            self._plugin_classes[provides] = cls
            logger.debug(f"Plugin {name} registered (module only, no instance)")
            return

        # Instantiate class for shared plugins
        try:
            instance = cls(config)

            # Set plugin manager reference
            if hasattr(instance, "set_plugin_manager"):
                instance.set_plugin_manager(self)

            # Initialize
            await instance.initialize()

            # Register by type using "provides" name (allows extension/replacement)
            # If another plugin already provides this, it gets replaced (last wins)
            registry_name = provides
            replaced = False

            if plugin_type == "runner":
                replaced = registry_name in self.runners
                self.runners[registry_name] = instance
            elif plugin_type == "channel":
                replaced = registry_name in self.channels
                self.channels[registry_name] = instance
            elif plugin_type == "service":
                replaced = registry_name in self.services
                self.services[registry_name] = instance
            elif plugin_type == "mcp":
                replaced = registry_name in self.mcp_providers
                self.mcp_providers[registry_name] = instance
            elif plugin_type == "theme":
                replaced = registry_name in self.themes
                self.themes[registry_name] = instance

            # Track provides mapping
            self._provides_map[provides] = name

            # For service-type plugins, also load MCP provider if specified
            if plugin_type == "service" and manifest.get("mcp_provider"):
                await self._load_mcp_provider_from_service(
                    name, plugin_path, manifest, config, instance
                )

            # Discover and register hooks from module
            self._register_hooks_from_module(module, name)

            # Load context injection if specified
            self._load_context_injection(name, plugin_path, manifest)

            # Execute on_plugin_loaded hook
            await self.hooks.execute(
                HookName.ON_PLUGIN_LOADED,
                {
                    "plugin_name": name,
                    "plugin_type": plugin_type,
                    "provides": provides,
                    "instance": instance,
                    "replaced": replaced,
                },
            )

            # Log with provides info if different from name
            if provides != name:
                action = "replaced" if replaced else "provides"
                logger.info(
                    f"Loaded plugin: {name} ({action} {provides}) "
                    f"v{manifest.get('version', '?')}"
                )
            else:
                logger.info(
                    f"Loaded plugin: {name} (type={plugin_type}) "
                    f"v{manifest.get('version', '?')}"
                )
        except Exception as exc:
            logger.error(f"Plugin {name} initialization failed: {exc}")
            try:
                import asyncio

                from core.notifications_client import send_notification

                asyncio.ensure_future(
                    send_notification(
                        category="plugin_error",
                        severity="error",
                        title=f"Plugin failed: {name}",
                        message=str(exc)[:200],
                        source=name,
                    )
                )
            except Exception:
                pass
            return

    async def _load_mcp_provider_from_service(
        self,
        name: str,
        plugin_path: Path,
        manifest: dict,
        config: dict,
        service_instance: BaseService,
    ) -> None:
        """Load MCP provider from a service-type plugin.

        This allows service plugins to also expose MCP tools without needing
        a separate plugin. The manifest should specify:
        - mcp_provider: Path to provider file (e.g., "provider.py")
        - mcp_provider_class: Class name of the MCP provider

        Args:
            name: Plugin name
            plugin_path: Plugin directory path
            manifest: Plugin manifest
            config: Plugin configuration
            service_instance: The already-loaded service instance
        """
        provider_file = manifest.get("mcp_provider")
        provider_class_name = manifest.get("mcp_provider_class")

        if not provider_file or not provider_class_name:
            return

        provider_path = plugin_path / provider_file
        if not provider_path.exists():
            logger.warning(f"MCP provider file not found: {provider_path}")
            return

        try:
            # Import the provider module
            provider_module_name = f"gridbear.plugins.{name}.mcp_provider"
            spec = importlib.util.spec_from_file_location(
                provider_module_name, provider_path
            )
            if spec is None or spec.loader is None:
                logger.error(f"Failed to load MCP provider spec for {name}")
                return

            provider_module = importlib.util.module_from_spec(spec)
            sys.modules[provider_module_name] = provider_module
            spec.loader.exec_module(provider_module)

            # Get provider class
            if not hasattr(provider_module, provider_class_name):
                logger.error(
                    f"MCP provider class {provider_class_name} not found in {name}"
                )
                return

            provider_cls = getattr(provider_module, provider_class_name)

            # Instantiate provider
            provider_instance = provider_cls(config)

            # Set plugin manager reference if supported
            if hasattr(provider_instance, "set_plugin_manager"):
                provider_instance.set_plugin_manager(self)

            # Link service to provider if supported
            if hasattr(provider_instance, "set_service"):
                provider_instance.set_service(service_instance)

            # Initialize provider
            await provider_instance.initialize()

            # Register as MCP provider using the provider's name attribute if available
            provider_name = getattr(provider_instance, "name", name)
            self.mcp_providers[provider_name] = provider_instance

            logger.info(f"Loaded MCP provider from service plugin: {name}")

        except Exception as e:
            logger.error(f"Failed to load MCP provider from {name}: {e}")

    def _register_hooks_from_module(self, module: Any, plugin_name: str) -> None:
        """Discover and register hooks defined in a module.

        Looks for functions decorated with @hook.
        """
        for item_name in dir(module):
            item = getattr(module, item_name)

            # Check if it's a function with hook decorators
            if callable(item) and hasattr(item, "_gridbear_hooks"):
                for hook_info in item._gridbear_hooks:
                    self.hooks.register(
                        hook_name=hook_info["hook_name"],
                        function=item,
                        priority=hook_info["priority"],
                        plugin_name=plugin_name,
                    )

    def _load_context_injection(
        self, name: str, plugin_path: Path, manifest: dict
    ) -> None:
        """Load context injection from plugin if specified.

        Plugins can provide system prompt context by specifying `context_injection`
        in their manifest pointing to a Python file with a `get_context()` function.
        """
        context_file = manifest.get("context_injection")
        if not context_file:
            return

        context_path = plugin_path / context_file
        if not context_path.exists():
            logger.warning(f"Context file not found for {name}: {context_path}")
            return

        try:
            # Import the context module
            context_module_name = f"gridbear.plugins.{name}.context"
            spec = importlib.util.spec_from_file_location(
                context_module_name, context_path
            )
            if spec is None or spec.loader is None:
                return

            context_module = importlib.util.module_from_spec(spec)
            sys.modules[context_module_name] = context_module
            spec.loader.exec_module(context_module)

            # Get context from get_context() function
            if hasattr(context_module, "get_context"):
                context = context_module.get_context()
                if context:
                    self._context_injections[name] = context
                    logger.debug(f"Loaded context injection from {name}")
        except Exception as e:
            logger.error(f"Failed to load context injection for {name}: {e}")

    async def _seed_context_skills(self) -> None:
        """Seed context skills from plugin .md files into the Skills DB.

        Called during load_all() after all plugins are loaded.
        Reads context_skills definitions from manifests and creates
        DB entries if they don't already exist.
        """
        skills_service = self.get_service("skills")
        if not skills_service:
            logger.warning(
                "Skills service not available, skipping context skill seeding"
            )
            return

        seeded = 0
        for name, manifest in self._manifests.items():
            context_skills = manifest.get("context_skills", [])
            plugin_path = self._path_resolver.resolve(name)
            if plugin_path is None:
                continue
            for skill_def in context_skills:
                md_path = plugin_path / skill_def["file"]
                if not md_path.exists():
                    logger.warning(f"Context skill file not found: {md_path}")
                    continue
                prompt = md_path.read_text(encoding="utf-8")
                created = await skills_service.seed_skill(
                    name=skill_def["name"],
                    title=skill_def["title"],
                    description=skill_def.get("description", ""),
                    prompt=prompt,
                    plugin_name=name,
                    category="context",
                )
                if created:
                    seeded += 1

        if seeded:
            logger.info(f"Seeded {seeded} new context skills")

    async def get_all_context_injections(self, unified_id: str | None = None) -> str:
        """Get all context injections concatenated.

        Reads from Skills DB (context_skills + user_skills) if available,
        falls back to in-memory dict for backward compatibility.

        Args:
            unified_id: If provided, also include per-user skills.

        Returns:
            Combined context string from all plugins with context injection
        """
        parts: list[str] = []
        skills_service = self.get_service("skills")
        if skills_service:
            enabled = list(self._manifests.keys())
            context_skills = await skills_service.get_context_skills(enabled)
            if context_skills:
                parts.extend(s["prompt"] for s in context_skills)

            # Per-user skills (created by or shared with this user)
            if unified_id:
                user_skills = await skills_service.get_user_skills(unified_id)
                if user_skills:
                    parts.extend(s["prompt"] for s in user_skills)

        # Fallback: old in-memory dict (backward compat during migration)
        if not parts and self._context_injections:
            parts.extend(self._context_injections.values())
        return "\n\n".join(parts)

    def get_runner(self, name: str | None = None) -> BaseRunner | None:
        """Get a runner by name, or the configured default, or the first available.

        Args:
            name: Optional runner name

        Returns:
            Runner instance or None
        """
        if name:
            return self.runners.get(name)
        # Try SystemConfig first, fall back to in-memory config
        default = None
        try:
            from core.system_config import SystemConfig

            default = SystemConfig.get_param_sync("default_runner")
        except Exception:
            pass
        if not default:
            default = getattr(self, "_plugins_config", {}).get("default_runner")
        if default and default in self.runners:
            return self.runners[default]
        return next(iter(self.runners.values()), None)

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name.

        Args:
            name: Channel name (platform)

        Returns:
            Channel instance or None
        """
        return self.channels.get(name)

    def get_service(self, name: str) -> BaseService | None:
        """Get a service by name.

        Args:
            name: Service name

        Returns:
            Service instance or None
        """
        return self.services.get(name)

    def get_service_by_interface(self, interface_class: type) -> BaseService | None:
        """Get a service by interface type instead of name.

        Allows the core to request services by contract (e.g. BaseMemoryService)
        rather than by plugin name. If multiple plugins implement the same
        interface, the last loaded one wins (consistent with 'provides').

        Args:
            interface_class: The interface class to look for (e.g. BaseSessionService)

        Returns:
            Service instance or None if no service implements the interface
        """
        matches = []
        for name, service in self.services.items():
            if isinstance(service, interface_class):
                matches.append((name, service))
        if len(matches) > 1:
            logger.warning(
                f"Multiple plugins implement {interface_class.__name__}: "
                f"{[m[0] for m in matches]}. Using last loaded: {matches[-1][0]}"
            )
        return matches[-1][1] if matches else None

    def get_mcp_provider(self, name: str) -> BaseMCPProvider | None:
        """Get an MCP provider by name.

        Args:
            name: Provider name

        Returns:
            MCP provider instance or None
        """
        return self.mcp_providers.get(name)

    def get_theme(self, name: str) -> BaseTheme | None:
        """Get a theme by name."""
        return self.themes.get(name)

    def get_active_theme(self) -> BaseTheme | None:
        """Get the currently active theme based on config.

        Note: Only works in the gridbear runtime container where load_all()
        has been called (which populates _plugins_config). In the admin
        container, use ui.jinja_env._theme_globals instead.

        Returns:
            Active theme instance or None if no theme is configured.
        """
        active_name = None
        try:
            from core.system_config import SystemConfig

            active_name = SystemConfig.get_param_sync("active_theme")
        except Exception:
            pass
        if not active_name:
            config = getattr(self, "_plugins_config", {})
            active_name = config.get("active_theme")
        if active_name:
            return self.themes.get(active_name)
        return None

    def get_plugin_module(self, name: str) -> Any | None:
        """Get a loaded plugin's module for importing classes.

        Args:
            name: Plugin name

        Returns:
            Module object or None

        Example:
            memory_module = plugin_manager.get_plugin_module("memory")
            MemoryService = memory_module.MemoryService
        """
        return self._modules.get(name)

    def get_plugin_class(self, name: str) -> type | None:
        """Get a plugin class for per-agent instantiation.

        Args:
            name: Plugin name or "provides" name

        Returns:
            Plugin class or None if not found
        """
        return self._plugin_classes.get(name)

    def get_plugin_manifest(self, name: str) -> dict | None:
        """Get manifest for a plugin.

        Args:
            name: Plugin name

        Returns:
            Manifest dict or None
        """
        # Try by name first
        if name in self._manifests:
            return self._manifests[name]
        # Try by provides name
        plugin_name = self._provides_map.get(name)
        if plugin_name:
            return self._manifests.get(plugin_name)
        return None

    def get_plugin_model(self, plugin_name: str) -> str | None:
        """Return model override declared by a plugin, or None."""
        manifest = self.get_plugin_manifest(plugin_name)
        if manifest:
            return manifest.get("model") or None
        return None

    def get_provider_info(self, provides_name: str) -> dict | None:
        """Get information about what plugin provides a given name.

        Args:
            provides_name: The provided service/runner/channel name

        Returns:
            Dict with plugin_name and manifest, or None
        """
        plugin_name = self._provides_map.get(provides_name)
        if plugin_name:
            return {
                "plugin_name": plugin_name,
                "manifest": self._manifests.get(plugin_name),
            }
        return None

    def generate_mcp_config(self) -> dict:
        """Generate MCP configuration from active providers (in-memory only).

        Returns:
            Dict with mcpServers configuration
        """
        servers = {}
        for name, provider in self.mcp_providers.items():
            server_config = provider.get_server_config()
            if isinstance(server_config, dict):
                # Provider may return single server or multiple
                if "command" in server_config or "url" in server_config:
                    # Single server config - use provider.name for server name
                    server_name = getattr(provider, "name", name)
                    servers[server_name] = server_config
                else:
                    # Multiple servers (e.g., Gmail accounts)
                    servers.update(server_config)
        return {"mcpServers": servers}

    def get_all_mcp_server_names(self) -> list[str]:
        """Get list of all MCP server names visible through the gateway.

        Includes:
          1. Names from registered ``mcp`` providers (and ``service``
             plugins that declare ``mcp_provider``).
          2. ``service`` plugins that declare ``virtual_tools`` in their
             manifest — these are exposed via the gateway's
             LocalToolProvider machinery (see
             ``core/mcp_gateway/tool_providers.py``) but are NOT in
             ``self.mcp_providers``. Without this, the admin form's
             "MCP Permissions" list and any other consumer relying on
             the runtime path would silently hide them
             (gridbeario/gridbear#152).
        """
        names = []
        for provider in self.mcp_providers.values():
            names.extend(provider.get_server_names())
        for plugin_name, manifest in self._manifests.items():
            if manifest.get("type") == "service" and manifest.get("virtual_tools"):
                vt_name = manifest.get("name") or plugin_name
                if vt_name not in names:
                    names.append(vt_name)
        return names

    def get_private_only_servers(self) -> set[str]:
        """Get MCP server names marked as private_only.

        These servers contain personal/sensitive data (email, documents)
        and should be filtered out in group chats.

        Returns:
            Set of server names that are private_only
        """
        result = set()
        for name, manifest in self._manifests.items():
            if manifest.get("type") == "mcp" and manifest.get("private_only"):
                result.add(name)
                provides = manifest.get("provides")
                if provides and provides != name:
                    result.add(provides)
        return result

    async def shutdown_all(self) -> None:
        """Shutdown all plugins in reverse order."""
        # Execute shutdown hook
        await self.hooks.execute(HookName.ON_SHUTDOWN, {"plugin_manager": self})

        # Channels first
        for name, channel in self.channels.items():
            try:
                await channel.shutdown()
                logger.debug(f"Shutdown channel: {name}")
            except Exception as e:
                logger.error(f"Error shutting down channel {name}: {e}")

        # Runners
        for name, runner in self.runners.items():
            try:
                await runner.shutdown()
                logger.debug(f"Shutdown runner: {name}")
            except Exception as e:
                logger.error(f"Error shutting down runner {name}: {e}")

        # MCP providers
        for name, provider in self.mcp_providers.items():
            try:
                await provider.shutdown()
                logger.debug(f"Shutdown MCP provider: {name}")
            except Exception as e:
                logger.error(f"Error shutting down MCP provider {name}: {e}")

        # Themes
        for name, theme in self.themes.items():
            try:
                await theme.shutdown()
                logger.debug(f"Shutdown theme: {name}")
            except Exception as e:
                logger.error(f"Error shutting down theme {name}: {e}")

        # Services last (others may depend on them)
        for name, service in self.services.items():
            try:
                await service.shutdown()
                logger.debug(f"Shutdown service: {name}")
            except Exception as e:
                logger.error(f"Error shutting down service {name}: {e}")

        logger.info("All plugins shutdown complete")

    def _load_config(self) -> dict:
        """Load plugin configuration from DB.

        Assembles a dict of {plugin_name: config_dict} from
        PluginRegistryEntry rows, plus global settings from SystemConfig.
        """
        from core.plugin_registry.models import PluginRegistryEntry
        from core.system_config import SystemConfig

        entries = PluginRegistryEntry.search_sync(
            [("state", "=", "installed"), ("enabled", "=", True)]
        )
        config = {}
        for entry in entries:
            config[entry["name"]] = entry.get("config") or {}

        # Global settings
        default_runner = SystemConfig.get_param_sync("default_runner")
        if default_runner:
            config["default_runner"] = default_runner
        active_theme = SystemConfig.get_param_sync("active_theme")
        if active_theme:
            config["active_theme"] = active_theme

        return config

    def list_available_plugins(self) -> list[dict]:
        """List all available plugins (discovered from directories).

        Returns:
            List of manifest info with _loaded flag
        """
        available = []
        plugins = self._discover_plugins()

        loaded_names = set(
            list(self.runners.keys())
            + list(self.channels.keys())
            + list(self.services.keys())
            + list(self.mcp_providers.keys())
            + list(self.themes.keys())
        )

        for name, manifest in plugins.items():
            manifest_copy = manifest.copy()
            manifest_copy["_loaded"] = name in loaded_names
            manifest_copy["_name"] = name
            available.append(manifest_copy)

        return available

    def get_plugin_config_schema(self, name: str) -> dict | None:
        """Get configuration schema for a plugin.

        Args:
            name: Plugin name

        Returns:
            Config schema dict or None
        """
        manifest = self._manifests.get(name)
        if manifest:
            return manifest.get("config_schema", {})

        # Try loading from disk via resolver
        plugin_path = self._path_resolver.resolve(name)
        if plugin_path:
            manifest_path = plugin_path / "manifest.json"
            if manifest_path.exists():
                with open(manifest_path) as f:
                    manifest = json.load(f)
                    return manifest.get("config_schema", {})

        return None

    async def reload_plugin(self, name: str) -> dict:
        """Reload a single plugin without restarting the application.

        Args:
            name: Plugin name to reload

        Returns:
            Dict with status and message

        Note:
            This reloads the plugin code and re-initializes it.
            Existing references to the old instance may become stale.
        """
        # Rebuild cache in case plugin files changed
        self._path_resolver.rebuild_cache()

        # Check if plugin exists on disk
        plugin_path = self._path_resolver.resolve(name)
        if plugin_path is None:
            return {"status": "error", "message": f"Plugin '{name}' not found"}

        manifest_path = plugin_path / "manifest.json"
        if not manifest_path.exists():
            return {"status": "error", "message": f"Plugin '{name}' not found"}

        # Load manifest
        with open(manifest_path) as f:
            manifest = json.load(f)

        plugin_type = manifest.get("type")
        provides = manifest.get("provides", name)

        # Shutdown existing instance if loaded
        old_instance = None
        if plugin_type == "service" and provides in self.services:
            old_instance = self.services[provides]
        elif plugin_type == "runner" and provides in self.runners:
            old_instance = self.runners[provides]
        elif plugin_type == "channel" and provides in self.channels:
            old_instance = self.channels[provides]
        elif plugin_type == "mcp" and provides in self.mcp_providers:
            old_instance = self.mcp_providers[provides]
        elif plugin_type == "theme" and provides in self.themes:
            old_instance = self.themes[provides]

        if old_instance:
            try:
                await old_instance.shutdown()
                logger.info(f"Shutdown old instance of plugin: {name}")
            except Exception as e:
                logger.warning(f"Error shutting down old plugin {name}: {e}")

        # Remove old module from sys.modules to force reload
        module_name = f"gridbear.plugins.{name}"
        short_module_name = f"plugins.{name}"

        for mod_name in [module_name, short_module_name]:
            if mod_name in sys.modules:
                del sys.modules[mod_name]

        # Remove old hooks from this plugin
        self.hooks.unregister_plugin(name)

        # Load fresh config
        config = self._load_config()
        plugin_config = config.get(name, {})

        # Re-load the plugin
        try:
            await self._load_plugin(name, plugin_config, manifest)
            logger.info(f"Reloaded plugin: {name}")
            return {
                "status": "success",
                "message": f"Plugin '{name}' reloaded successfully",
                "type": plugin_type,
                "provides": provides,
            }
        except Exception as e:
            logger.error(f"Failed to reload plugin {name}: {e}")
            return {"status": "error", "message": str(e)}

    async def load_new_plugin(self, name: str) -> dict:
        """Load a new plugin that wasn't loaded at startup.

        Args:
            name: Plugin name to load

        Returns:
            Dict with status and message
        """
        # Check if already loaded
        if name in self._manifests:
            return await self.reload_plugin(name)

        # Rebuild cache in case plugin was just added
        self._path_resolver.rebuild_cache()

        # Check if plugin exists on disk
        plugin_path = self._path_resolver.resolve(name)
        if plugin_path is None:
            return {"status": "error", "message": f"Plugin '{name}' not found"}

        manifest_path = plugin_path / "manifest.json"
        if not manifest_path.exists():
            return {"status": "error", "message": f"Plugin '{name}' not found"}

        # Load manifest
        with open(manifest_path) as f:
            manifest = json.load(f)

        # Load config
        config = self._load_config()
        plugin_config = config.get(name, {})

        # Load the plugin
        try:
            await self._load_plugin(name, plugin_config, manifest)
            logger.info(f"Loaded new plugin: {name}")
            return {
                "status": "success",
                "message": f"Plugin '{name}' loaded successfully",
                "type": manifest.get("type"),
                "provides": manifest.get("provides", name),
            }
        except Exception as e:
            logger.error(f"Failed to load plugin {name}: {e}")
            return {"status": "error", "message": str(e)}

    # ── Plugin lifecycle (install / uninstall / enable / disable) ─

    async def install_plugin(self, name: str) -> list[str]:
        """Install a plugin. Returns list of warnings."""
        if not self._registry:
            raise RuntimeError("Plugin registry not available")
        manifest = self._path_resolver.discover_all().get(name)
        if not manifest:
            raise ValueError(f"Plugin '{name}' not found on disk")
        return await self._registry.install(name, manifest, None)

    async def uninstall_plugin(self, name: str) -> None:
        """Uninstall a plugin (remove config, secrets, data)."""
        if not self._registry:
            raise RuntimeError("Plugin registry not available")
        await self._registry.uninstall(name)

    async def enable_plugin(self, name: str) -> None:
        """Enable an installed plugin (requires restart to take effect)."""
        if not self._registry:
            raise RuntimeError("Plugin registry not available")
        await self._registry.set_enabled(name, True)

    async def disable_plugin(self, name: str) -> None:
        """Disable an installed plugin (requires restart to take effect)."""
        if not self._registry:
            raise RuntimeError("Plugin registry not available")
        await self._registry.set_enabled(name, False)
