"""Theme management routes for Admin UI."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.logging_config import logger
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


def _discover_themes() -> tuple[list[dict], str]:
    """Discover all available themes (enabled or not) with metadata.

    Returns (themes_list, active_theme_name).
    """
    from core.registry import get_path_resolver
    from core.system_config import SystemConfig
    from ui.plugin_helpers import get_enabled_plugins, load_plugin_config
    from ui.theme_utils import load_theme_instance

    resolver = get_path_resolver()
    if not resolver:
        return [], ""

    # Get active theme from SystemConfig
    active_theme = SystemConfig.get_param_sync("active_theme", "") or ""
    enabled = set(get_enabled_plugins())

    # Discover ALL plugins and filter by type == theme
    all_manifests = resolver.discover_all()
    themes = []

    for plugin_name, manifest in all_manifests.items():
        if manifest.get("type") != "theme":
            continue

        plugin_path = resolver.resolve(plugin_name)
        if not plugin_path:
            continue

        theme_config = load_plugin_config(plugin_name)
        instance = load_theme_instance(plugin_name, manifest, plugin_path, theme_config)

        if instance is None:
            themes.append(
                {
                    "name": plugin_name,
                    "display_name": manifest.get(
                        "display_name", plugin_name.replace("-", " ").title()
                    ),
                    "description": manifest.get("description", ""),
                    "author": manifest.get("author", ""),
                    "preview_image": "",
                    "accent_color": "",
                    "enabled": plugin_name in enabled,
                }
            )
            continue

        metadata = instance.get_metadata()
        metadata["name"] = plugin_name
        metadata["enabled"] = plugin_name in enabled

        # Build preview image URL if relative path given
        preview = metadata.get("preview_image", "")
        if preview and not preview.startswith("/"):
            metadata["preview_image"] = f"/static/theme/{plugin_name}/{preview}"

        themes.append(metadata)

    # Sort: active first, then alphabetical
    themes.sort(key=lambda t: (t["name"] != active_theme, t["name"]))

    return themes, active_theme


@router.get("/", response_class=HTMLResponse)
async def themes_page(request: Request, _=Depends(require_login)):
    """Theme manager — list all available themes."""
    themes, active_theme = _discover_themes()

    return get_templates().TemplateResponse(
        request,
        "themes.html",
        get_template_context(
            request,
            themes=themes,
            active_theme=active_theme,
        ),
    )


@router.post("/activate")
async def activate_theme(
    request: Request,
    theme: str = Form(""),
    _=Depends(require_login),
):
    """Activate a theme — updates SystemConfig and rebuilds template loader."""
    from core.system_config import SystemConfig

    # Set active theme (empty string = default/no theme)
    new_value = theme if theme else None
    try:
        await SystemConfig.set_param("active_theme", new_value)
    except Exception as exc:
        logger.error("Failed to set active_theme: %s", exc)
        return RedirectResponse("/themes/?error=save_failed", status_code=303)

    # If theme not enabled, enable it via registry
    if theme:
        try:
            from core.plugin_registry.models import PluginRegistryEntry

            entry = await PluginRegistryEntry.get(name=theme)
            if entry and not entry.get("enabled"):
                await PluginRegistryEntry.write(entry["id"], enabled=True)
                if entry["state"] == "available":
                    await PluginRegistryEntry.write(entry["id"], state="installed")
        except Exception as exc:
            logger.warning("Could not enable theme '%s': %s", theme, exc)

    # Rebuild template loader to pick up new theme overrides
    from ui.jinja_env import rebuild_template_loader

    rebuild_template_loader()

    return RedirectResponse("/themes/?saved=1", status_code=303)
