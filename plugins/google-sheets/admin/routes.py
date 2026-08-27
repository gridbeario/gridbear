"""Google Sheets MCP plugin admin routes.

SA management is handled by the google-sa plugin.
This file only handles Sheets-specific settings.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.plugin_helpers import load_plugin_config, save_plugin_config
from ui.routes.auth import require_login
from ui.routes.plugins import get_plugin_info, get_template_context

router = APIRouter(prefix="/plugins/google-sheets", tags=["google-sheets"])


def _get_plugin_metadata() -> dict:
    """Plugin metadata for auto-sidebar."""
    return get_plugin_info("google-sheets") or {}


def _get_config() -> dict:
    defaults = {"drive_folder_id": "", "enabled_tools": ""}
    return {**defaults, **load_plugin_config("google-sheets")}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def google_sheets_index(request: Request, user: dict = Depends(require_login)):
    """Google Sheets plugin configuration page."""
    config = _get_config()
    plugin_info = get_plugin_info("google-sheets")

    return templates.TemplateResponse(
        request,
        "plugins/google_sheets.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name="google-sheets",
            config=config,
        ),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    drive_folder_id: str = Form(""),
    enabled_tools: str = Form(""),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Save plugin configuration."""
    validate_csrf_token(request, csrf_token)

    config = _get_config()
    config["drive_folder_id"] = drive_folder_id.strip()
    config["enabled_tools"] = enabled_tools.strip()
    save_plugin_config("google-sheets", config)

    return RedirectResponse(
        url="/plugins/google-sheets?saved=settings", status_code=303
    )
