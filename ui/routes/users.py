from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ui.auth.database import auth_db
from ui.config_manager import ConfigManager
from ui.jinja_env import templates
from ui.routes.auth import require_login
from ui.utils.channels import get_available_channels

router = APIRouter()
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
        "active_company_id": getattr(request.state, "active_company_id", None),
        "user_companies": getattr(request.state, "user_companies", []),
        "active_company_name": getattr(request.state, "active_company_name", None),
        **kwargs,
    }


def _get_valid_channel_names() -> set[str]:
    """Get set of valid channel names for route validation."""
    return {ch["name"] for ch in get_available_channels()}


@router.get("/", response_class=HTMLResponse)
async def users_page(request: Request, _: bool = Depends(require_login)):
    config = ConfigManager()
    users = auth_db.get_all_users()

    # Build channels list with their authorized users
    channels = get_available_channels()
    for ch in channels:
        ch["users"] = config.get_channel_users(ch["name"])

    # Merge platform identities into each user (locale is already on User)
    user_identities = config.get_user_identities()
    for u in users:
        uid = u["username"]
        u["platforms"] = user_identities.get(uid, {})
        if not u.get("locale"):
            u["locale"] = "en"

    return templates.TemplateResponse(
        "users.html",
        get_template_context(
            request,
            channels=channels,
            users=users,
            available_locales=config.get_available_locales(),
        ),
    )


# User identities (cross-platform linking)
@router.post("/identity/add")
async def add_user_identity(
    request: Request,
    unified_id: str = Form(...),
    locale: str = Form(default="en"),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    unified_id = unified_id.strip().lower()
    if unified_id:
        # Read platform usernames dynamically from form data
        form_data = await request.form()
        for ch_name in _get_valid_channel_names():
            value = form_data.get(ch_name, "").strip()
            if value:
                config.add_user_identity(unified_id, ch_name, value)
        if locale.strip():
            config.set_user_locale(unified_id, locale.strip())
    return RedirectResponse(url="/users", status_code=303)


@router.post("/identity/{unified_id}/set-locale")
async def set_user_locale(
    request: Request,
    unified_id: str,
    locale: str = Form(...),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.set_user_locale(unified_id, locale)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/identity/{unified_id}/remove-platform")
async def remove_identity_platform(
    request: Request,
    unified_id: str,
    platform: str = Form(...),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.remove_user_identity(unified_id, platform)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/identity/{unified_id}/delete")
async def delete_user_identity(
    request: Request,
    unified_id: str,
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.remove_user_identity(unified_id)
    return RedirectResponse(url="/users", status_code=303)


# --- Portal Users (admin_auth.db) ---

MIN_PASSWORD_LENGTH = 8


@router.post("/portal/create")
async def create_portal_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    is_superadmin: str = Form(default=""),
    _: bool = Depends(require_login),
):
    """Create a new user."""
    from ui.routes.auth import hash_password

    username = username.strip().lower()
    if not username or len(password) < MIN_PASSWORD_LENGTH:
        return RedirectResponse(url="/users?error=invalid_input", status_code=303)

    existing = auth_db.get_user_by_username(username)
    if existing:
        return RedirectResponse(url="/users?error=username_exists", status_code=303)

    try:
        auth_db.create_user(
            username=username,
            password_hash=hash_password(password),
            display_name=display_name.strip() or None,
            is_superadmin=is_superadmin == "1",
            email=email.strip() or None,
        )
    except psycopg.errors.UniqueViolation as exc:
        if getattr(exc.diag, "constraint_name", None) == "idx_users_email_lower":
            return RedirectResponse(url="/users?error=email_exists", status_code=303)
        return RedirectResponse(url="/users?error=username_exists", status_code=303)

    return RedirectResponse(url="/users", status_code=303)


@router.post("/portal/{user_id}/update")
async def update_portal_user(
    request: Request,
    user_id: int,
    display_name: str = Form(default=""),
    email: str = Form(default=""),
    is_superadmin: str = Form(default=""),
    is_active: str = Form(default=""),
    _: bool = Depends(require_login),
):
    """Update a user."""
    try:
        auth_db.update_user(
            user_id,
            display_name=display_name.strip() or None,
            is_superadmin=is_superadmin == "1",
            is_active=is_active == "1",
            email=email.strip() or None,
        )
    except psycopg.errors.UniqueViolation:
        return RedirectResponse(url="/users?error=email_exists", status_code=303)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/portal/{user_id}/reset-password")
async def reset_portal_user_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    _: bool = Depends(require_login),
):
    """Reset a portal user's password."""
    from ui.routes.auth import hash_password

    if len(new_password) < MIN_PASSWORD_LENGTH:
        return RedirectResponse(url="/users?error=password_short", status_code=303)

    auth_db.update_user(user_id, password_hash=hash_password(new_password))
    return RedirectResponse(url="/users", status_code=303)


@router.post("/portal/{user_id}/delete")
async def delete_portal_user(
    request: Request,
    user_id: int,
    admin_user: dict = Depends(require_login),
):
    """Delete a portal user."""
    # Prevent self-deletion
    if user_id == admin_user.get("id"):
        return RedirectResponse(url="/users?error=self_delete", status_code=303)

    auth_db.delete_user(user_id)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/portal/{user_id}/generate-invite")
async def generate_user_invite(
    request: Request,
    user_id: int,
    _: bool = Depends(require_login),
):
    """Generate an invite token and return the link (JSON)."""
    from core.models.user import User
    from ui.auth.invite import generate_token

    user = User.get_sync(id=user_id)
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    raw_token = generate_token(user_id, purpose="invite")
    base_url = str(request.base_url).rstrip("/")
    token_url = f"{base_url}/auth/setup-password?token={raw_token}"
    has_email = bool(user.get("email"))

    return JSONResponse(
        {
            "token_url": token_url,
            "username": user["username"],
            "has_email": has_email,
        }
    )


@router.post("/portal/{user_id}/send-invite-email")
async def send_invite_email_route(
    request: Request,
    user_id: int,
    _: bool = Depends(require_login),
):
    """Send the invite email for an already-generated token."""
    from core.models.user import User
    from ui.auth.invite import generate_token, send_invite_email

    user = User.get_sync(id=user_id)
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    raw_token = generate_token(user_id, purpose="invite")
    base_url = str(request.base_url).rstrip("/")
    token_url = f"{base_url}/auth/setup-password?token={raw_token}"

    sent = await send_invite_email(user, token_url)
    return JSONResponse({"sent": sent})


# --- Generic platform routes (MUST be last to avoid matching /identity/*, /portal/*) ---


@router.post("/{platform}/add")
async def add_channel_user(
    request: Request,
    platform: str,
    user_id: str = Form(default=""),
    username: str = Form(default=""),
    _: bool = Depends(require_login),
):
    if platform not in _get_valid_channel_names():
        return RedirectResponse(url="/users", status_code=303)
    config = ConfigManager()
    uid = int(user_id) if user_id.strip().isdigit() else None
    uname = username.strip() if username.strip() else None
    if uid or uname:
        config.add_channel_user(platform, user_id=uid, username=uname)
    return RedirectResponse(url="/users", status_code=303)


@router.post("/{platform}/remove")
async def remove_channel_user(
    request: Request,
    platform: str,
    user_id: str = Form(default=""),
    username: str = Form(default=""),
    _: bool = Depends(require_login),
):
    if platform not in _get_valid_channel_names():
        return RedirectResponse(url="/users", status_code=303)
    config = ConfigManager()
    uid = int(user_id) if user_id.strip().isdigit() else None
    uname = username.strip() if username.strip() else None
    if uid or uname:
        config.remove_channel_user(platform, user_id=uid, username=uname)
    return RedirectResponse(url="/users", status_code=303)
