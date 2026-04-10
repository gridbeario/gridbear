"""Agent management routes for Admin UI."""

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ui.routes.auth import require_login

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
AVATARS_DIR = BASE_DIR / "ui" / "static" / "avatars"
ALLOWED_AVATAR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB

router = APIRouter()

# Import templates lazily to avoid circular imports
_templates = None


def get_templates():
    """Get templates instance from app module."""
    global _templates
    if _templates is None:
        from ui.app import templates

        _templates = templates
    return _templates


def get_known_users() -> dict:
    """Get known users by platform from user identities.

    Returns:
        Dict with {platform: [usernames]} for all available channels
    """
    from ui.config_manager import ConfigManager
    from ui.utils.channels import get_available_channels

    config = ConfigManager()
    identities = config.get_user_identities()

    # Initialize with all available channels, build prefix lookup
    channels = get_available_channels()
    result = {ch["name"]: [] for ch in channels}
    prefix_map = {ch["name"]: ch.get("username_prefix", "") for ch in channels}

    for unified_id, platforms in identities.items():
        for platform, username in platforms.items():
            if platform in result and username:
                prefix = prefix_map.get(platform, "")
                if prefix and not username.startswith(prefix):
                    username = f"{prefix}{username}"
                if username not in result[platform]:
                    result[platform].append(username)

    # Sort alphabetically
    for platform in result:
        result[platform].sort(key=str.lower)

    return result


def get_available_plugins() -> dict:
    """Get available plugins by type."""
    from core.registry import get_path_resolver
    from ui.plugin_helpers import get_enabled_plugins

    resolver = get_path_resolver()

    result = {"mcp": [], "services": [], "channels": []}

    enabled = get_enabled_plugins()
    if not enabled:
        return result

    all_manifests = resolver.discover_all() if resolver else {}

    for plugin_name in enabled:
        manifest = all_manifests.get(plugin_name)
        if manifest is None:
            continue
        plugin_type = manifest.get("type", "")

        if plugin_type == "mcp":
            mcp_names = get_mcp_server_names_for_plugin(plugin_name, manifest)
            result["mcp"].extend(mcp_names)
        elif plugin_type == "service":
            result["services"].append(plugin_name)
            if manifest.get("mcp_provider"):
                mcp_names = get_mcp_server_names_for_plugin(plugin_name, manifest)
                result["mcp"].extend(mcp_names)
        elif plugin_type == "channel":
            result["channels"].append(plugin_name)

    return result


def get_mcp_server_names_for_plugin(plugin_name: str, manifest: dict) -> list[str]:
    """Get actual MCP server names for a plugin.

    Reads naming configuration from the plugin manifest:
    - mcp_server_name: static override (e.g., "odoo-mcp")
    - mcp_naming: "per_account" or "per_tenant" for dynamic multi-server names
    - mcp_name_template: pattern like "gmail-{account}" or "ms365-{tenant}"

    Falls back to plugin_name if no manifest field is set.
    """
    mcp_naming = manifest.get("mcp_naming")

    if mcp_naming == "per_account":
        from ui.config_manager import ConfigManager

        template = manifest.get("mcp_name_template", f"{plugin_name}-{{account}}")
        config = ConfigManager()
        gmail_accounts = config.get_gmail_accounts()
        names = []
        for emails in gmail_accounts.values():
            for email in emails:
                names.append(template.format(account=email))
        return names if names else [plugin_name]

    if mcp_naming == "per_tenant":
        template = manifest.get("mcp_name_template", f"{plugin_name}-{{tenant}}")
        from ui.plugin_helpers import load_plugin_config

        plugin_cfg = load_plugin_config(plugin_name)
        tenants = plugin_cfg.get("tenants", [])
        names = [template.format(tenant=t["name"]) for t in tenants if t.get("name")]
        if names:
            return names
        return [plugin_name]

    # Static server name: read from manifest or default to plugin_name
    return [manifest.get("mcp_server_name", plugin_name)]


def load_agent(agent_id: str) -> dict | None:
    """Load agent config from DB."""
    from core.models.agent_config import AgentConfigRecord

    record = AgentConfigRecord.get_sync(id=agent_id)
    if not record:
        return None
    return dict(record)


def save_agent(agent_id: str, config: dict) -> None:
    """Save agent config to DB."""
    from core.models.agent_config import AgentConfigRecord

    data = {k: v for k, v in config.items() if k != "id"}
    AgentConfigRecord.create_or_update_sync(
        _conflict_fields=("id",),
        id=agent_id,
        **data,
    )


def list_agents() -> list[dict]:
    """List all agents from DB."""
    from core.models.agent_config import AgentConfigRecord

    records = AgentConfigRecord.search_sync(order="id ASC")
    agents = []
    for record in records:
        config = dict(record)
        channels = config.get("channels") or {}
        plugins = config.get("plugins") or {}
        agents.append(
            {
                "id": config.get("id", ""),
                "name": config.get("name", ""),
                "description": config.get("description", ""),
                "channels": list(channels.keys()) if isinstance(channels, dict) else [],
                "plugins": plugins.get("enabled", [])
                if isinstance(plugins, dict)
                else [],
                "mcp_permissions": config.get("mcp_permissions", []),
                "locale": config.get("locale", "en"),
                "avatar": config.get("avatar", ""),
                "max_tools": config.get("max_tools"),
                "tool_loading": config.get("tool_loading", "full"),
            }
        )
    return agents


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


@router.get("/", response_class=HTMLResponse)
async def agents_list(request: Request, _: dict = Depends(require_login)):
    """List all agents."""
    from ui.utils.channels import get_channel_ui_map

    agents = list_agents()

    # Tool call counts per agent (7 days)
    tool_counts: dict[str, int] = {}
    try:
        from core.registry import get_database

        db = get_database()
        if db:
            rows = await db.fetch_all(
                "SELECT agent_name, COUNT(*) AS cnt "
                "FROM public.tool_usage "
                "WHERE called_at > NOW() - INTERVAL '7 days' "
                "GROUP BY agent_name"
            )
            tool_counts = {r["agent_name"]: r["cnt"] for r in rows}
    except Exception:
        pass

    return get_templates().TemplateResponse(
        "agents/list.html",
        get_template_context(
            request,
            agents=agents,
            channel_ui=get_channel_ui_map(),
            tool_counts=tool_counts,
        ),
    )


@router.post("/reload")
async def reload_agents(request: Request, _: dict = Depends(require_login)):
    """Request reload of all agents (hot reload - no restart)."""
    import time

    reload_file = BASE_DIR / "data" / "agent_reload.json"
    reload_file.parent.mkdir(parents=True, exist_ok=True)

    # Write reload request
    request_data = {
        "status": "pending",
        "requested_at": time.time(),
        "requested_by": "admin_ui",
    }

    with open(reload_file, "w") as f:
        json.dump(request_data, f, indent=2)

    # Wait a bit and check result
    await asyncio.sleep(3)

    try:
        with open(reload_file) as f:
            result = json.load(f)
    except Exception:
        result = {"status": "unknown"}

    if result.get("status") == "completed":
        return RedirectResponse(url="/agents/?reload=success", status_code=303)
    elif result.get("status") == "error":
        errors = result.get("errors", [])
        error_msg = errors[0] if errors else "Unknown error"
        return RedirectResponse(
            url=f"/agents/?reload=error&msg={error_msg}", status_code=303
        )
    else:
        return RedirectResponse(url="/agents/?reload=pending", status_code=303)


@router.post("/restart")
async def restart_gridbear(request: Request, _: dict = Depends(require_login)):
    """Request full restart of GridBear (reloads plugin config and all configs).

    Warning: If there's a configuration error, the service won't restart.
    """
    import time

    restart_file = BASE_DIR / "data" / "restart_requested.json"
    restart_file.parent.mkdir(parents=True, exist_ok=True)

    # Write restart request
    request_data = {
        "status": "pending",
        "requested_at": time.time(),
        "requested_by": "admin_ui",
    }

    with open(restart_file, "w") as f:
        json.dump(request_data, f, indent=2)

    # Redirect to dashboard — user can see uptime reset to confirm restart
    return RedirectResponse(url="/?restart=requested", status_code=303)


def _get_runner_models() -> dict[str, list[tuple[str, str]]]:
    """Get available models from all enabled runner plugins.

    Reads from ModelsRegistry first (proper display names), then falls back
    to manifest.json enum (with naive .capitalize() labels).

    Returns:
        Dict mapping runner name to list of (value, label) tuples.
        E.g. {"claude": [("sonnet", "Sonnet"), ...], "gemini": [...]}
    """
    from core.registry import get_models_registry, get_path_resolver
    from ui.plugin_helpers import get_enabled_plugins

    resolver = get_path_resolver()

    try:
        all_manifests = resolver.discover_all() if resolver else {}
        registry = get_models_registry()
        result = {}

        for plugin_name in get_enabled_plugins():
            manifest = all_manifests.get(plugin_name)
            if manifest is None:
                continue
            if manifest.get("type") != "runner":
                continue

            # Prefer registry (has proper display names)
            if registry:
                models = registry.get_for_ui(plugin_name)
                if models:
                    result[plugin_name] = models
                    continue

            # Fallback: manifest enum with .capitalize() labels
            model_schema = manifest.get("config_schema", {}).get("model", {})
            models = model_schema.get("enum", [])
            result[plugin_name] = [(m, m.capitalize()) for m in models]

        return result
    except Exception:
        pass

    return {}


def _load_tts_class(provider: str):
    """Load a TTS plugin class by provider name from its manifest."""
    import importlib.util

    plugin_dir = BASE_DIR / "plugins" / provider
    manifest_path = plugin_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("provides", manifest.get("type")) != "tts":
        return None
    class_name = manifest.get("class_name")
    entry_point = manifest.get("entry_point", "service.py")
    if not class_name:
        return None
    service_path = plugin_dir / entry_point
    if not service_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"gridbear.plugins.{provider}.service",
        service_path,
        submodule_search_locations=[str(service_path.parent)],
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name, None)


@router.get("/api/voices/{provider}")
async def list_voices(
    provider: str,
    locale: str | None = None,
    _: dict = Depends(require_login),
):
    """List available voices for a TTS provider."""
    tts_class = _load_tts_class(provider)
    if not tts_class:
        raise HTTPException(404, f"Unknown TTS provider '{provider}'")

    try:
        instance = tts_class({})
        await instance.initialize()
        voices = await instance.list_voices(locale=locale)
        return JSONResponse(voices)
    except Exception as e:
        raise HTTPException(500, f"Failed to list voices: {e}")


@router.get("/new", response_class=HTMLResponse)
async def new_agent(request: Request, _: dict = Depends(require_login)):
    """Show new agent form."""
    from ui.utils.channels import get_available_channels

    available_plugins = get_available_plugins()
    known_users = get_known_users()

    return get_templates().TemplateResponse(
        "agents/edit.html",
        get_template_context(
            request,
            agent=None,
            available_mcp=available_plugins["mcp"],
            available_services=available_plugins["services"],
            available_channels=available_plugins["channels"],
            channels=get_available_channels(),
            known_users=known_users,
            runner_models=_get_runner_models(),
            is_new=True,
        ),
    )


@router.get("/{agent_id}", response_class=HTMLResponse)
async def edit_agent(request: Request, agent_id: str, _: dict = Depends(require_login)):
    """Show agent edit form."""
    from ui.utils.channels import get_available_channels

    agent = load_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    available_plugins = get_available_plugins()
    known_users = get_known_users()

    return get_templates().TemplateResponse(
        "agents/edit.html",
        get_template_context(
            request,
            agent=agent,
            available_mcp=available_plugins["mcp"],
            available_services=available_plugins["services"],
            available_channels=available_plugins["channels"],
            channels=get_available_channels(),
            known_users=known_users,
            runner_models=_get_runner_models(),
            is_new=False,
        ),
    )


def _delete_avatar_files(agent_id: str) -> None:
    """Delete all avatar files for an agent (any extension)."""
    for ext in ALLOWED_AVATAR_EXTENSIONS:
        path = AVATARS_DIR / f"{agent_id}{ext}"
        if path.exists():
            path.unlink()


@router.post("/{agent_id}")
async def save_agent_config(
    request: Request,
    agent_id: str,
    name: str = Form(...),
    description: str = Form(""),
    personality: str = Form(""),
    locale: str = Form("en"),
    timezone: str = Form("Europe/Rome"),
    model: str = Form(""),
    runner: str = Form(""),
    fallback_runner: str = Form(""),
    voice_provider: str = Form(""),
    voice_id: str = Form(""),
    voice_language: str = Form(""),
    image_provider: str = Form(""),
    email_account: str = Form(""),
    email_sender_name: str = Form(""),
    email_from_alias: str = Form(""),
    email_signature: str = Form(""),
    mcp_permissions: list[str] = Form([]),
    plugins_enabled: list[str] = Form([]),
    # Tool management
    max_tools: str = Form(""),
    tool_loading: str = Form("full"),
    form_agent_id: str = Form(None, alias="agent_id"),
    # Avatar
    avatar: UploadFile | None = File(None),
    remove_avatar: str = Form(""),
    _: dict = Depends(require_login),
):
    """Save agent configuration."""
    from ui.utils.channels import get_available_channels

    # For new agents, use the ID from the form
    if agent_id == "new" and form_agent_id:
        agent_id = form_agent_id

    # Load existing config to merge with (preserves fields not in form)
    existing_config = load_agent(agent_id) or {}

    # Handle avatar upload/removal
    avatar_value = existing_config.get("avatar", "")

    if remove_avatar == "on":
        _delete_avatar_files(agent_id)
        avatar_value = ""
    elif avatar and avatar.filename:
        ext = Path(avatar.filename).suffix.lower()
        if ext not in ALLOWED_AVATAR_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"File type {ext} not allowed")
        content = await avatar.read()
        if len(content) > MAX_AVATAR_SIZE:
            raise HTTPException(status_code=400, detail="File too large (max 2MB)")
        # Remove old avatar (extension might change)
        _delete_avatar_files(agent_id)
        # Save new avatar
        AVATARS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{agent_id}{ext}"
        (AVATARS_DIR / filename).write_bytes(content)
        avatar_value = filename

    config = {
        "id": agent_id,
        "name": name,
        "description": description,
        "personality": personality + "\n" if personality else "",
        "locale": locale,
        "timezone": timezone,
    }

    if runner.strip():
        config["runner"] = runner.strip()
    if fallback_runner.strip():
        config["fallback_runner"] = fallback_runner.strip()
    if model.strip():
        config["model"] = model.strip()

    # Tool management
    if max_tools.strip():
        try:
            config["max_tools"] = int(max_tools)
        except ValueError:
            pass
    if tool_loading in ("full", "search"):
        config["tool_loading"] = tool_loading

    if avatar_value:
        config["avatar"] = avatar_value

    # Channels - read dynamically from form data
    form_data = await request.form()
    channels = existing_config.get("channels", {}).copy()
    for ch in get_available_channels():
        ch_name = ch["name"]
        token_key = f"{ch_name}_token_secret"
        users_key = f"{ch_name}_allowed_users"
        token_value = form_data.get(token_key, "")
        if token_value:
            ch_config = {"token_secret": token_value}
            allowed = form_data.getlist(users_key)
            if allowed:
                ch_config["allowed_users"] = [u.strip() for u in allowed if u.strip()]
            channels[ch_name] = ch_config

    config["channels"] = channels

    # Voice
    if voice_provider:
        config["voice"] = {
            "provider": voice_provider,
            "voice_id": voice_id or "nova",
            "language": voice_language or "en-US",
        }

    # Image
    if image_provider:
        config["image"] = {"provider": image_provider}

    # Email settings
    if email_account.strip():
        email_cfg = {"account": email_account.strip()}
        if email_sender_name.strip():
            email_cfg["sender_name"] = email_sender_name.strip()
        if email_from_alias.strip():
            email_cfg["from_alias"] = email_from_alias.strip()
        if email_signature.strip():
            email_cfg["signature"] = email_signature.strip()
        config["email"] = email_cfg

    # MCP permissions - always save what form sends (allows deselecting all)
    config["mcp_permissions"] = mcp_permissions

    # Plugins - always save what form sends
    config["plugins"] = {"enabled": plugins_enabled}

    save_agent(agent_id, config)

    # Signal bot container to reload this agent
    gridbear_url = os.getenv("GRIDBEAR_URL", "http://gridbear:8000")
    secret = os.getenv("INTERNAL_API_SECRET", "")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{gridbear_url}/api/agents/{agent_id}/reload",
                headers={"Authorization": f"Bearer {secret}"},
            )
    except Exception as exc:
        logger.warning("Failed to signal agent reload for %s: %s", agent_id, exc)

    return RedirectResponse(url="/agents/", status_code=303)


@router.post("/{agent_id}/delete")
async def delete_agent(
    request: Request, agent_id: str, _: dict = Depends(require_login)
):
    """Delete an agent."""
    from core.models.agent_config import AgentConfigRecord

    AgentConfigRecord.delete_sync(agent_id)

    # Clean up avatar files
    _delete_avatar_files(agent_id)

    # Signal bot container to reload all agents
    gridbear_url = os.getenv("GRIDBEAR_URL", "http://gridbear:8000")
    secret = os.getenv("INTERNAL_API_SECRET", "")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{gridbear_url}/api/agents/{agent_id}/reload",
                headers={"Authorization": f"Bearer {secret}"},
            )
    except Exception as exc:
        logger.warning(
            "Failed to signal agent reload after delete %s: %s", agent_id, exc
        )

    return RedirectResponse(url="/agents/", status_code=303)
