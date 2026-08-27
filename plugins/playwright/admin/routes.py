"""Admin routes for Playwright plugin."""

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.config_manager import ConfigManager
from ui.jinja_env import templates
from ui.plugin_helpers import (
    get_plugin_template_context,
    load_plugin_config,
    save_plugin_config,
)
from ui.routes.auth import require_login

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def get_users_with_permission() -> list[str]:
    """Get list of users who have playwright permission."""
    config = ConfigManager()
    all_permissions = config.get_all_user_permissions()
    users = []
    for user, perms in all_permissions.items():
        if "playwright" in perms:
            users.append(user)
    return users


def get_all_users() -> list[str]:
    """Get list of all known users."""
    config = ConfigManager()
    return list(config.get_user_identities().keys())


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def playwright_settings(request: Request, _=Depends(require_login)):
    """Playwright settings page."""
    config = load_plugin_config("playwright")
    users_with_access = get_users_with_permission()
    all_users = get_all_users()
    users_without_access = [u for u in all_users if u not in users_with_access]

    return templates.TemplateResponse(
        request,
        "playwright.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            config=config,
            users_with_access=users_with_access,
            users_without_access=users_without_access,
        ),
    )


@router.post("/grant")
async def grant_access(
    request: Request,
    username: str = Form(...),
    _=Depends(require_login),
):
    """Grant playwright access to a user."""
    config = ConfigManager()
    current_perms = config.get_user_permissions(username) or []

    if "playwright" not in current_perms:
        current_perms.append("playwright")
        config.set_user_permissions(username, current_perms)

    return RedirectResponse(url="/plugin/playwright?granted=1", status_code=303)


@router.post("/revoke")
async def revoke_access(
    request: Request,
    username: str = Form(...),
    _=Depends(require_login),
):
    """Revoke playwright access from a user."""
    config = ConfigManager()
    current_perms = config.get_user_permissions(username) or []

    if "playwright" in current_perms:
        current_perms.remove("playwright")
        config.set_user_permissions(username, current_perms)

    return RedirectResponse(url="/plugin/playwright?revoked=1", status_code=303)


@router.post("/settings")
async def save_settings(
    request: Request,
    headless: bool = Form(True),
    _=Depends(require_login),
):
    """Save Playwright settings."""
    config = load_plugin_config("playwright")
    config["headless"] = headless
    save_plugin_config("playwright", config)
    return RedirectResponse(url="/plugin/playwright?saved=1", status_code=303)
