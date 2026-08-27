"""Plugin configuration routes."""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.routes.auth import require_login
from ui.secrets_manager import secrets_manager

router = APIRouter(prefix="/plugins")
BASE_DIR = Path(__file__).resolve().parent.parent.parent
ADMIN_DIR = Path(__file__).resolve().parent.parent


def get_enabled_plugins_by_type() -> dict:
    """Get enabled plugins grouped by type."""
    from ui.app import get_enabled_plugins_by_type as _get

    return _get()


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context with enabled plugins and menus."""
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


def get_plugin_info(plugin_name: str) -> dict | None:
    """Get plugin manifest and current config."""
    from core.registry import get_plugin_path
    from ui.plugin_helpers import load_plugin_config

    plugin_path = get_plugin_path(plugin_name)
    if plugin_path is None:
        return None

    manifest_path = plugin_path / "manifest.json"
    if not manifest_path.exists():
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Get current config from DB
    current_config = load_plugin_config(plugin_name)

    # Merge with defaults from schema
    config_schema = manifest.get("config_schema", {})
    merged_config = {}
    secrets_status = {}

    # Handle both flat schema and JSON Schema format
    # JSON Schema has "properties" key, flat schema has direct field definitions
    schema_properties = config_schema.get("properties", config_schema)

    for key, schema in schema_properties.items():
        # Skip JSON Schema meta-keys
        if key in ("type", "properties", "definitions", "required", "$schema"):
            continue
        # Ensure schema is a dict before accessing .get()
        if not isinstance(schema, dict):
            continue
        if schema.get("type") == "secret":
            # Check secret status
            env_key = schema.get("env", key.upper())
            if secrets_manager.is_available() and secrets_manager.exists(env_key):
                secrets_status[env_key] = "encrypted"
            elif os.getenv(env_key):
                secrets_status[env_key] = "env"
            else:
                secrets_status[env_key] = "missing"
        else:
            default_value = schema.get("default", "")
            merged_config[key] = current_config.get(key, default_value)

    # Parse dependencies from manifest
    raw_deps = manifest.get("dependencies", [])
    if isinstance(raw_deps, list):
        parsed_deps = {"required": raw_deps, "optional": []}
    elif isinstance(raw_deps, dict):
        parsed_deps = {
            "required": raw_deps.get("required", []),
            "optional": raw_deps.get("optional", []),
        }
    else:
        parsed_deps = {"required": [], "optional": []}

    return {
        "name": manifest.get("name", plugin_name),
        "display_name": manifest.get("name", plugin_name),
        "version": manifest.get("version", "1.0.0"),
        "type": manifest.get("type", "unknown"),
        "description": manifest.get("description", ""),
        "provides": manifest.get("provides"),
        "model": manifest.get("model"),
        "config_schema": config_schema,
        "current_config": merged_config,
        "secrets_status": secrets_status,
        "dependencies": parsed_deps,
    }


def save_plugin_config(plugin_name: str, new_config: dict) -> None:
    """Save plugin configuration to DB."""
    from ui.plugin_helpers import save_plugin_config as _save

    _save(plugin_name, new_config)


async def _get_registry():
    """Get a PluginRegistry, ensuring DB is populated.

    The UI container may start before gridbear runs the migration.
    If the registry table is empty, sync with disk + migrate from config.
    """
    from core.plugin_registry import PluginRegistry, PluginRegistryEntry
    from core.registry import get_path_resolver

    registry = PluginRegistry()

    # Ensure the table is populated
    count = await PluginRegistryEntry.count()
    config_path = BASE_DIR / "config" / "plugins.json"
    if count == 0:
        resolver = get_path_resolver()
        disk = resolver.discover_all() if resolver else {}
        if disk:
            await registry.migrate_from_config(config_path, disk)
            await registry.sync_with_disk(disk)

    # Migrate config from file if not done yet
    await registry.migrate_config_from_file(config_path)

    return registry


async def get_all_plugins() -> list[dict]:
    """Get all plugins with their registry state."""
    from core.registry import get_path_resolver

    resolver = get_path_resolver()
    all_manifests = resolver.discover_all() if resolver else {}

    # Get DB state from registry (direct, no plugin_manager needed)
    registry_entries = {}
    try:
        registry = await _get_registry()
        for entry in await registry.get_all():
            registry_entries[entry["name"]] = entry
    except Exception:
        pass  # DB not available yet

    # Fall back if no registry data: treat all disk plugins as available
    if not registry_entries:
        for name in all_manifests:
            registry_entries[name] = {
                "state": "available",
                "enabled": False,
                "installed_at": None,
            }

    plugins = []
    for name, manifest in all_manifests.items():
        entry = registry_entries.get(name, {})
        plugins.append(
            {
                "name": name,
                "display_name": manifest.get("name", name),
                "version": manifest.get("version", "1.0.0"),
                "type": manifest.get("type", "unknown"),
                "description": manifest.get("description", ""),
                "provides": manifest.get("provides"),
                "model": manifest.get("model"),
                "state": entry.get("state", "available"),
                "enabled": entry.get("enabled", False),
                "installed_at": entry.get("installed_at"),
            }
        )

    # Add not_available entries (in DB but missing from disk)
    for name, entry in registry_entries.items():
        if name not in all_manifests and entry.get("state") == "not_available":
            plugins.append(
                {
                    "name": name,
                    "display_name": name,
                    "version": entry.get("version", ""),
                    "type": entry.get("plugin_type", "unknown"),
                    "description": "",
                    "state": "not_available",
                    "enabled": False,
                }
            )

    type_order = {"runner": 0, "channel": 1, "service": 2, "mcp": 3, "theme": 4}
    plugins.sort(key=lambda p: (type_order.get(p["type"], 99), p["name"]))
    return plugins


@router.get("/", response_class=HTMLResponse)
async def plugins_list(request: Request, _: bool = Depends(require_login)):
    """List all plugins grouped by state."""
    plugins = await get_all_plugins()

    return templates.TemplateResponse(
        request,
        "plugins/list.html",
        get_template_context(
            request,
            plugins=plugins,
        ),
    )


@router.post("/reload-all")
async def reload_all_plugins(
    request: Request,
    _: bool = Depends(require_login),
):
    """Request hot reload of all plugins."""
    import time

    reload_file = BASE_DIR / "data" / "reload_requests.json"
    reload_file.parent.mkdir(parents=True, exist_ok=True)

    with open(reload_file, "w") as f:
        json.dump(
            [
                {
                    "plugin": "__all__",
                    "timestamp": time.time(),
                    "status": "pending",
                }
            ],
            f,
            indent=2,
        )

    return RedirectResponse(url="/plugins/?reload_requested=1", status_code=303)


@router.get("/paths", response_class=HTMLResponse)
async def plugin_paths_page(request: Request, _: bool = Depends(require_login)):
    """Show plugin paths management page.

    Plugin paths are now configured via GRIDBEAR_PLUGIN_PATHS env var
    (comma-separated) and EXTRA_PLUGINS_DIRS (colon-separated).
    """
    builtin_dir = BASE_DIR / "plugins"

    all_dirs: list[Path] = [builtin_dir]

    # GRIDBEAR_PLUGIN_PATHS env var (primary)
    gp = os.environ.get("GRIDBEAR_PLUGIN_PATHS", "").strip()
    if gp:
        for path_str in gp.split(","):
            path_str = path_str.strip()
            if path_str:
                all_dirs.append(Path(path_str))

    # EXTRA_PLUGINS_DIRS env var (legacy compat)
    extra = os.environ.get("EXTRA_PLUGINS_DIRS", "").strip()
    if extra:
        for path_str in extra.split(":"):
            path_str = path_str.strip()
            if path_str:
                all_dirs.append(Path(path_str))

    paths = []
    for d in all_dirs:
        plugin_count = 0
        if d.exists() and d.is_dir():
            plugin_count = sum(
                1 for p in d.iterdir() if p.is_dir() and (p / "manifest.json").exists()
            )
        paths.append(
            {
                "path": str(d),
                "exists": d.exists(),
                "plugin_count": plugin_count,
                "is_builtin": d.resolve() == builtin_dir.resolve(),
                "source": "env",
            }
        )

    return templates.TemplateResponse(
        request,
        "plugins/paths.html",
        get_template_context(request, plugin_paths=paths),
    )


@router.post("/rescan")
async def rescan_plugins(
    request: Request,
    csrf_token: str = Form(...),
    _: bool = Depends(require_login),
):
    """Rescan plugin directories and update registry."""
    validate_csrf_token(request, csrf_token)
    from core.registry import get_path_resolver, get_plugin_manager

    resolver = get_path_resolver()
    if resolver:
        resolver.rebuild_cache()

    pm = get_plugin_manager()
    if pm and pm.registry:
        all_manifests = resolver.discover_all() if resolver else {}
        await pm.registry.sync_with_disk(all_manifests)

    return RedirectResponse(url="/plugins/?rescanned=1", status_code=303)


def _get_runner_context(plugin_name: str) -> dict:
    """Build template context for runner plugin extras (models + CLI auth).

    Reads cli_meta and models_refreshable from the plugin's manifest.json,
    so runner plugins declare their own capabilities.
    """
    from core.registry import get_models_registry, get_plugin_path

    # Read manifest for cli_meta and models_refreshable
    cli_meta: dict = {}
    models_refreshable = False
    plugin_path = get_plugin_path(plugin_name)
    if plugin_path:
        manifest_path = plugin_path / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)
            cli_meta = manifest.get("cli_meta", {})
            models_refreshable = manifest.get("models_refreshable", False)

    registry = get_models_registry()
    model_info = {
        "models": [],
        "last_updated": None,
        "source": None,
        "has_refresh": models_refreshable,
    }
    if registry:
        meta = registry.get_metadata(plugin_name)
        if meta:
            model_info["models"] = meta.get("models", [])
            model_info["last_updated"] = meta.get("last_updated")
            model_info["source"] = meta.get("source")

    return {
        "runner_model_info": model_info,
        "runner_has_cli": bool(cli_meta),
        "runner_cli_name": cli_meta.get("cli_name", ""),
        "runner_auth_actions": cli_meta.get("auth_actions", []),
        "runner_token_endpoint": cli_meta.get("token_endpoint", "token"),
        "runner_token_hint": cli_meta.get("token_hint", ""),
        "runner_token_placeholder": cli_meta.get("token_placeholder", ""),
    }


@router.get("/{plugin_name}", response_class=HTMLResponse)
async def plugin_config(
    request: Request, plugin_name: str, _: bool = Depends(require_login)
):
    """Show plugin configuration page."""
    plugin_info = get_plugin_info(plugin_name)

    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Get dependents from runtime plugin_manager if available
    plugin_dependents = []
    try:
        from core.registry import get_plugin_manager

        pm = get_plugin_manager()
        if pm:
            dep_info = pm.get_dependents(plugin_name)
            # get_dependents returns {"required_by": [...], "optional_by": [...]}
            plugin_dependents = dep_info.get("required_by", []) + dep_info.get(
                "optional_by", []
            )
    except Exception:
        pass

    # Runner-specific context (models + CLI auth)
    runner_ctx = {}
    if plugin_info.get("type") == "runner":
        runner_ctx = _get_runner_context(plugin_name)

    return templates.TemplateResponse(
        request,
        "plugins/config.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name=plugin_name,
            encryption_available=secrets_manager.is_available(),
            plugin_dependencies=plugin_info.get("dependencies", {}),
            plugin_dependents=plugin_dependents,
            **runner_ctx,
        ),
    )


@router.post("/{plugin_name}", response_class=HTMLResponse)
async def save_plugin_config_route(
    request: Request,
    plugin_name: str,
    _: bool = Depends(require_login),
):
    """Save plugin configuration."""
    plugin_info = get_plugin_info(plugin_name)
    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Get form data
    form_data = await request.form()

    # Build config from form
    new_config = {}
    config_schema = plugin_info["config_schema"]

    for key, schema in config_schema.items():
        value = form_data.get(key, "")
        field_type = schema.get("type", "string")

        # Type conversion
        if field_type == "integer":
            new_config[key] = int(value) if value else schema.get("default", 0)
        elif field_type == "boolean":
            new_config[key] = value == "on" or value == "true"
        elif field_type == "object":
            # For objects, try to parse as JSON
            try:
                new_config[key] = (
                    json.loads(value) if value else schema.get("default", {})
                )
            except json.JSONDecodeError:
                new_config[key] = schema.get("default", {})
        elif field_type == "array":
            try:
                new_config[key] = (
                    json.loads(value) if value else schema.get("default", [])
                )
            except json.JSONDecodeError:
                new_config[key] = schema.get("default", [])
        else:
            new_config[key] = value if value else schema.get("default", "")

    save_plugin_config(plugin_name, new_config)

    return RedirectResponse(url=f"/plugins/{plugin_name}?saved=1", status_code=303)


@router.post("/{plugin_name}/install")
async def install_plugin(
    request: Request,
    plugin_name: str,
    csrf_token: str = Form(...),
    _: bool = Depends(require_login),
):
    """Install a plugin."""
    validate_csrf_token(request, csrf_token)
    from core.registry import get_plugin_path

    plugin_path = get_plugin_path(plugin_name)
    if not plugin_path:
        raise HTTPException(status_code=404, detail="Plugin not found on disk")

    manifest_path = plugin_path / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Plugin manifest not found")

    with open(manifest_path) as f:
        manifest = json.load(f)

    try:
        registry = await _get_registry()
        warnings = await registry.install(plugin_name, manifest, None)
        qs = f"?installed={plugin_name}"
        if warnings:
            qs += "&warnings=" + "|".join(warnings)
        return RedirectResponse(url=f"/plugins/{qs}", status_code=303)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_name}/uninstall")
async def uninstall_plugin(
    request: Request,
    plugin_name: str,
    csrf_token: str = Form(...),
    _: bool = Depends(require_login),
):
    """Uninstall a plugin (removes config, secrets, data)."""
    validate_csrf_token(request, csrf_token)
    try:
        registry = await _get_registry()
        await registry.uninstall(plugin_name)
        return RedirectResponse(
            url=f"/plugins/?uninstalled={plugin_name}", status_code=303
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_name}/enable")
async def enable_plugin(
    request: Request,
    plugin_name: str,
    csrf_token: str = Form(...),
    _: bool = Depends(require_login),
):
    """Enable an installed plugin (requires restart)."""
    validate_csrf_token(request, csrf_token)
    try:
        registry = await _get_registry()
        await registry.set_enabled(plugin_name, True)
        return RedirectResponse(url=f"/plugins/?enabled={plugin_name}", status_code=303)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_name}/disable")
async def disable_plugin(
    request: Request,
    plugin_name: str,
    csrf_token: str = Form(...),
    _: bool = Depends(require_login),
):
    """Disable an installed plugin (requires restart)."""
    validate_csrf_token(request, csrf_token)
    try:
        registry = await _get_registry()
        await registry.set_enabled(plugin_name, False)
        return RedirectResponse(
            url=f"/plugins/?disabled={plugin_name}", status_code=303
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_name}/secret/{env_key}")
async def save_plugin_secret(
    request: Request,
    plugin_name: str,
    env_key: str,
    _: bool = Depends(require_login),
):
    """Save a secret for a plugin."""
    if not secrets_manager.is_available():
        raise HTTPException(status_code=400, detail="Encryption not available")

    form_data = await request.form()

    # Find the secret field key that matches this env_key
    plugin_info = get_plugin_info(plugin_name)
    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Find the form field name
    secret_value = None
    for key, schema in plugin_info["config_schema"].items():
        if schema.get("type") == "secret":
            schema_env_key = schema.get("env", key.upper())
            if schema_env_key == env_key:
                secret_value = form_data.get(f"secret_{key}")
                break

    if secret_value and secret_value.strip():
        secrets_manager.set(
            env_key, secret_value.strip(), description=f"Secret for {plugin_name}"
        )
        return RedirectResponse(
            url=f"/plugins/{plugin_name}?secret_saved=1", status_code=303
        )

    return RedirectResponse(url=f"/plugins/{plugin_name}", status_code=303)


@router.post("/{plugin_name}/secret/{env_key}/delete")
async def delete_plugin_secret(
    request: Request,
    plugin_name: str,
    env_key: str,
    _: bool = Depends(require_login),
):
    """Delete a secret for a plugin."""
    if not secrets_manager.is_available():
        raise HTTPException(status_code=400, detail="Encryption not available")

    secrets_manager.delete(env_key)
    return RedirectResponse(
        url=f"/plugins/{plugin_name}?secret_deleted=1", status_code=303
    )


@router.post("/{plugin_name}/reload")
async def reload_plugin(
    request: Request,
    plugin_name: str,
    _: bool = Depends(require_login),
):
    """Request hot reload of a plugin.

    Writes a reload request to a shared file that gridbear monitors.
    """
    # Verify plugin exists
    plugin_info = get_plugin_info(plugin_name)
    if not plugin_info:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Write reload request to shared data directory
    reload_file = BASE_DIR / "data" / "reload_requests.json"
    reload_file.parent.mkdir(parents=True, exist_ok=True)

    import time

    requests = []
    if reload_file.exists():
        try:
            with open(reload_file) as f:
                requests = json.load(f)
        except (json.JSONDecodeError, OSError):
            requests = []

    # Add new request
    requests.append(
        {
            "plugin": plugin_name,
            "timestamp": time.time(),
            "status": "pending",
        }
    )

    with open(reload_file, "w") as f:
        json.dump(requests, f, indent=2)

    return RedirectResponse(
        url=f"/plugins/{plugin_name}?reload_requested=1", status_code=303
    )


@router.post("/{plugin_name}/{agent_id}/trigger")
async def trigger_plugin_action(
    request: Request,
    plugin_name: str,
    agent_id: str,
    _: dict = Depends(require_login),
):
    """Generic plugin action trigger — delegates to the bot's internal API.

    Handles POST /plugins/{plugin}/peggy/trigger by proxying to the bot
    container's internal API.
    """
    import httpx

    bot_url = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
    api_secret = os.getenv("INTERNAL_API_SECRET", "")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{bot_url}/api/{plugin_name}/{agent_id}/trigger",
                headers={"Authorization": f"Bearer {api_secret}"},
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse({"ok": False, "error": "Bot unreachable"}, status_code=503)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
