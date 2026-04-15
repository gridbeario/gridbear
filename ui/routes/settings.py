"""Admin routes for general settings."""

import os
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config.logging_config import logger
from ui.config_manager import ConfigManager
from ui.jinja_env import templates
from ui.routes.auth import require_login
from ui.secrets_manager import secrets_manager

GRIDBEAR_URL = os.getenv("GRIDBEAR_INTERNAL_URL", "http://gridbear:8000")
INTERNAL_SECRET = os.getenv("INTERNAL_API_SECRET", "")

ADMIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = ADMIN_DIR.parent

router = APIRouter(prefix="/settings", tags=["settings"])


def get_template_context(request: Request, **kwargs) -> dict:
    """Get base template context."""
    plugins = getattr(request.state, "plugins", {})
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


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(require_login)):
    """General settings page."""
    from core.system_config import SystemConfig
    from ui import tts_service

    encryption_available = secrets_manager.is_available()
    key_source = secrets_manager.get_key_source() if encryption_available else None

    config = ConfigManager()
    identities = config.get_all_unified_ids()
    bot_identity = config.get_bot_identity()
    bot_email_settings = config.get_bot_email_settings()

    tts_providers = tts_service.get_available_providers()
    current_tts_provider = config.get_webchat_tts_provider()

    dump_prompts = bool(SystemConfig.get_param_sync("dump_prompts"))

    return templates.TemplateResponse(
        "settings.html",
        get_template_context(
            request,
            encryption_available=encryption_available,
            key_source=key_source,
            identities=identities,
            bot_identity=bot_identity,
            bot_email_settings=bot_email_settings,
            tts_providers=tts_providers,
            current_tts_provider=current_tts_provider,
            dump_prompts=dump_prompts,
        ),
    )


@router.post("/bot")
async def save_bot_settings(
    request: Request,
    bot_identity: str = Form(""),
    email_enabled: str = Form(""),
    email_interval: int = Form(5),
    email_label: str = Form("INBOX"),
    email_instructions: str = Form(""),
    auto_reply_allowed: str = Form(""),
    notification_username: str = Form(""),
    notification_chat_id: str = Form(""),
    sender_name: str = Form("GridBear"),
    sender_alias: str = Form(""),
    signature: str = Form(""),
    _=Depends(require_login),
):
    """Save bot identity and email settings."""
    config = ConfigManager()

    # Save bot identity
    config.set_bot_identity(bot_identity if bot_identity else None)

    # Parse chat_id if provided
    chat_id = None
    if notification_chat_id.strip():
        try:
            chat_id = int(notification_chat_id.strip())
        except ValueError:
            pass

    # Save email settings
    config.set_bot_email_settings(
        {
            "enabled": email_enabled == "on",
            "check_interval_minutes": email_interval,
            "label": email_label.strip() or "INBOX",
            "instructions": email_instructions.strip(),
            "auto_reply_allowed": auto_reply_allowed.strip(),
            "notification_username": notification_username.strip().lstrip("@"),
            "notification_chat_id": chat_id,
            "sender_name": sender_name.strip() or "GridBear",
            "sender_alias": sender_alias.strip(),
            "signature": signature,
        }
    )

    return RedirectResponse("/settings?saved=bot", status_code=303)


@router.post("/debug")
async def save_debug_settings(
    request: Request,
    dump_prompts: str = Form(""),
    _=Depends(require_login),
):
    """Save debug/diagnostics settings."""
    from core.system_config import SystemConfig

    SystemConfig.set_param_sync("dump_prompts", dump_prompts == "on")
    return RedirectResponse("/settings?saved=debug", status_code=303)


@router.post("/tts")
async def save_tts_settings(
    request: Request,
    tts_provider: str = Form("browser"),
    _=Depends(require_login),
):
    """Save WebChat TTS provider."""
    from ui import tts_service

    config = ConfigManager()
    config.set_webchat_tts_provider(tts_provider)
    tts_service.clear_cache()

    return RedirectResponse("/settings?saved=tts", status_code=303)


# ── Internal API proxy ────────────────────────────────────────────────
# The admin UI (this container) proxies requests to the gridbear container
# internal API for model management and CLI auth.


async def _proxy_to_internal(method: str, path: str, body: dict | None = None) -> dict:
    """Forward a request to the gridbear internal API."""
    url = f"{GRIDBEAR_URL}{path}"
    headers = {"Authorization": f"Bearer {INTERNAL_SECRET}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            else:
                resp = await client.post(url, json=body or {}, headers=headers)
            return resp.json()
    except httpx.ConnectError:
        return {
            "ok": False,
            "error": "GridBear service unavailable",
            "code": "unavailable",
        }
    except Exception:
        logger.exception("Internal API proxy error (%s %s)", method, path)
        return {"ok": False, "error": "Internal error", "code": "internal_error"}


# Models proxy endpoints


@router.get("/api/models/{runner}", response_class=JSONResponse)
async def proxy_get_models(runner: str, _=Depends(require_login)):
    """Proxy: get model list for a runner."""
    return JSONResponse(await _proxy_to_internal("GET", f"/api/{runner}/models"))


@router.post("/api/models/{runner}", response_class=JSONResponse)
async def proxy_set_models(runner: str, request: Request, _=Depends(require_login)):
    """Proxy: update model list for a runner."""
    body = await request.json()
    return JSONResponse(await _proxy_to_internal("POST", f"/api/{runner}/models", body))


@router.post("/api/models/{runner}/refresh", response_class=JSONResponse)
async def proxy_refresh_models(runner: str, _=Depends(require_login)):
    """Proxy: refresh model list from upstream API."""
    return JSONResponse(
        await _proxy_to_internal("POST", f"/api/{runner}/models/refresh")
    )


# CLI auth proxy endpoints


@router.get("/api/auth/{runner}/status", response_class=JSONResponse)
async def proxy_auth_status(runner: str, _=Depends(require_login)):
    """Proxy: get CLI auth status."""
    return JSONResponse(await _proxy_to_internal("GET", f"/api/{runner}/auth/status"))


@router.post("/api/auth/{runner}/logout", response_class=JSONResponse)
async def proxy_auth_logout(runner: str, _=Depends(require_login)):
    """Proxy: CLI logout."""
    return JSONResponse(await _proxy_to_internal("POST", f"/api/{runner}/auth/logout"))


@router.post("/api/auth/{runner}/login", response_class=JSONResponse)
async def proxy_auth_login(runner: str, _=Depends(require_login)):
    """Proxy: start CLI login flow."""
    return JSONResponse(await _proxy_to_internal("POST", f"/api/{runner}/auth/login"))


@router.post("/api/auth/{runner}/code", response_class=JSONResponse)
async def proxy_auth_code(runner: str, request: Request, _=Depends(require_login)):
    """Proxy: submit OAuth authorization code."""
    body = await request.json()
    return JSONResponse(
        await _proxy_to_internal("POST", f"/api/{runner}/auth/code", body)
    )


@router.post("/api/auth/{runner}/token", response_class=JSONResponse)
async def proxy_auth_token(runner: str, request: Request, _=Depends(require_login)):
    """Proxy: submit auth token."""
    body = await request.json()
    return JSONResponse(
        await _proxy_to_internal("POST", f"/api/{runner}/auth/token", body)
    )


@router.post("/api/auth/{runner}/api-key", response_class=JSONResponse)
async def proxy_auth_api_key(runner: str, request: Request, _=Depends(require_login)):
    """Proxy: submit API key."""
    body = await request.json()
    return JSONResponse(
        await _proxy_to_internal("POST", f"/api/{runner}/auth/api-key", body)
    )
