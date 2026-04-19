"""Shared Jinja2 template environment for the admin UI.

All route modules should import `templates` from here instead of
creating their own Jinja2Templates instance.
"""

from pathlib import Path

from jinja2 import ChoiceLoader, FileSystemLoader
from starlette.templating import Jinja2Templates

from core.__version__ import __version__
from core.i18n import get_language, get_translation
from ui.csrf import get_csrf_token

ADMIN_DIR = Path(__file__).resolve().parent
BASE_DIR = ADMIN_DIR.parent

templates = Jinja2Templates(directory=ADMIN_DIR / "templates")

# Global template variables
templates.env.globals["csrf_token"] = get_csrf_token
templates.env.globals["version"] = __version__


def _jinja_translate(message: str) -> str:
    """Translate a UI string using the current request language."""
    return get_translation("ui", message, get_language())


templates.env.globals["_"] = _jinja_translate


def _plugin_enabled(name: str) -> bool:
    """Template helper: is the given plugin installed and enabled?

    Used to guard hardcoded menu links in templates (e.g. the user
    portal sidebar) against enterprise plugins that may not be
    present on this install.
    """
    from ui.plugin_helpers import get_enabled_plugins

    return name in get_enabled_plugins()


templates.env.globals["plugin_enabled"] = _plugin_enabled


def _load_active_theme_data() -> dict:
    """Load theme data from the active theme plugin.

    Returns dict with css_vars, custom_css, tailwind_config, metadata.
    Called once and cached in the Jinja2 globals.
    """
    # Get active theme name from DB (SystemConfig)
    active_name = None
    try:
        from core.system_config import SystemConfig

        active_name = SystemConfig.get_param_sync("active_theme")
    except Exception:
        pass

    if not active_name:
        return {}

    from core.registry import get_path_resolver

    resolver = get_path_resolver()
    if not resolver:
        return {}

    all_manifests = resolver.discover_all()
    manifest = all_manifests.get(active_name)
    if not manifest or manifest.get("type") != "theme":
        return {}

    plugin_path = resolver.resolve(active_name)
    if not plugin_path:
        return {}

    from ui.plugin_helpers import load_plugin_config
    from ui.theme_utils import load_theme_instance

    theme_config = load_plugin_config(active_name)
    instance = load_theme_instance(active_name, manifest, plugin_path, theme_config)
    if instance is None:
        return {}

    metadata = instance.get_metadata()
    return {
        "theme_css_vars": instance.get_css_variables(),
        "theme_custom_css": instance.get_custom_css(),
        "theme_tailwind_config": instance.get_tailwind_config(),
        "theme_metadata": metadata,
        "theme_font_imports": metadata.get("font_imports", []),
        "active_theme_name": active_name,
    }


# Cached theme data — populated by rebuild_template_loader()
_theme_globals: dict = {}


def rebuild_template_loader() -> None:
    """Rebuild the Jinja2 template loader to include theme template overrides.

    Called on startup and when the active theme changes.  The active theme's
    templates/ directory is prepended so its templates take priority.
    Also refreshes the theme CSS globals available to all templates.
    """
    global _theme_globals
    loaders = []

    from core.registry import get_path_resolver

    resolver = get_path_resolver()

    # Get active theme from DB (SystemConfig)
    active_name = None
    try:
        from core.system_config import SystemConfig

        active_name = SystemConfig.get_param_sync("active_theme")
    except Exception:
        pass

    # 1) Active theme templates — highest priority
    if active_name and resolver:
        plugin_path = resolver.resolve(active_name)
        if plugin_path:
            theme_templates = plugin_path / "templates"
            if theme_templates.is_dir():
                loaders.append(FileSystemLoader(str(theme_templates)))

    # 2) Plugin admin template directories — between theme and default
    # Get enabled plugins from DB registry
    enabled = []
    try:
        from core.plugin_registry.models import PluginRegistryEntry

        entries = PluginRegistryEntry.search_sync(
            [("state", "=", "installed"), ("enabled", "=", True)]
        )
        enabled = [e["name"] for e in entries]
    except Exception:
        pass

    all_manifests = resolver.discover_all() if resolver else {}
    if resolver:
        for plugin_name in enabled:
            manifest = all_manifests.get(plugin_name, {})
            if manifest.get("type") == "theme":
                continue
            ppath = resolver.resolve(plugin_name)
            if not ppath:
                continue
            for subdir in ("admin/templates", "templates"):
                tdir = ppath / subdir
                if tdir.is_dir():
                    loaders.append(FileSystemLoader(str(tdir)))
                    break

    # Default templates directory (always last)
    loaders.append(FileSystemLoader(str(ADMIN_DIR / "templates")))

    templates.env.loader = ChoiceLoader(loaders)

    # Refresh theme globals — available in ALL templates without
    # needing to be passed explicitly in the template context
    _theme_globals = _load_active_theme_data()
    templates.env.globals["theme_css_vars"] = _theme_globals.get("theme_css_vars")
    templates.env.globals["theme_custom_css"] = _theme_globals.get("theme_custom_css")
    templates.env.globals["theme_tailwind_config"] = _theme_globals.get(
        "theme_tailwind_config"
    )
    templates.env.globals["theme_metadata"] = _theme_globals.get("theme_metadata")
    templates.env.globals["theme_font_imports"] = _theme_globals.get(
        "theme_font_imports", []
    )
    templates.env.globals["active_theme_name"] = _theme_globals.get("active_theme_name")


# Filters


def _datefmt(value, fmt="%Y-%m-%d %H:%M"):
    """Format a datetime or ISO string for display in templates."""
    if value is None:
        return "-"
    if hasattr(value, "strftime"):
        return value.strftime(fmt)
    return str(value)


def _isoformat(value):
    """Convert datetime to ISO string, pass strings through."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


templates.env.filters["datefmt"] = _datefmt
templates.env.filters["isoformat"] = _isoformat
