from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.config_manager import ConfigManager
from ui.jinja_env import templates
from ui.routes.auth import require_login

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def permissions_page(request: Request, _: bool = Depends(require_login)):
    from ui.auth.models import AdminUser
    from ui.utils.channels import get_available_channels, get_channel_ui_map
    from ui.utils.mcp_servers import get_available_mcp_servers

    config = ConfigManager()
    permissions = config.get_all_user_permissions()
    available_servers = get_available_mcp_servers()

    # Last-chance fallback: union with any server names referenced by
    # existing user/group permissions (covers older installs whose plugin
    # registry predates the MCP plugin registration).
    if not available_servers:
        seen = set()
        for servers in permissions.values():
            seen.update(servers)
        for servers in config.get_all_group_permissions().values():
            seen.update(servers)
        available_servers = sorted(seen)

    # Build user list from AdminUser (username + display_name)
    admin_users = AdminUser.search_sync()
    user_display = {}
    for u in admin_users:
        uid = u.get("username")
        if uid:
            user_display[uid] = u.get("display_name") or uid

    # Also include unified_ids from identities (users without admin accounts)
    identities = config.get_user_identities()
    for uid in identities:
        if uid not in user_display:
            user_display[uid] = uid

    # Ensure all users with existing permissions appear in user_display
    for uid in permissions:
        if uid not in user_display:
            user_display[uid] = uid

    # Build platform badges for each unified_id
    channels = get_available_channels()
    channel_users = {}
    for ch in channels:
        channel_users[ch["name"]] = set(
            u.lower() for u in config.get_channel_users(ch["name"]).get("usernames", [])
        )

    user_platforms = {}
    for uid in permissions:
        platforms = []
        identity = identities.get(uid, {})
        for ch_name in channel_users:
            ch_username = identity.get(ch_name)
            if ch_username and ch_username.lower() in channel_users[ch_name]:
                platforms.append(ch_name)
        user_platforms[uid] = platforms

    # Group permissions
    memory_groups = config.get_memory_groups()
    group_permissions = config.get_all_group_permissions()

    plugin_menus = getattr(request.state, "plugin_menus", [])
    return templates.TemplateResponse(
        request,
        "permissions.html",
        {
            "request": request,
            "plugin_menus": plugin_menus,
            "permissions": permissions,
            "available_servers": available_servers,
            "user_platforms": user_platforms,
            "user_display": user_display,
            "channel_ui": get_channel_ui_map(),
            "memory_groups": memory_groups,
            "group_permissions": group_permissions,
        },
    )


@router.post("/save")
async def save_permissions(
    request: Request,
    unified_id: str = Form(...),
    servers: List[str] = Form(default=[]),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.set_user_permissions(unified_id, servers)
    return RedirectResponse(url="/permissions/", status_code=303)


@router.post("/delete/{unified_id:path}")
async def delete_permissions(
    request: Request,
    unified_id: str,
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.delete_user_permissions(unified_id)
    return RedirectResponse(url="/permissions/", status_code=303)


@router.post("/group/save")
async def save_group_permissions(
    request: Request,
    group_name: str = Form(...),
    servers: List[str] = Form(default=[]),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    existing_groups = config.get_memory_groups()
    if group_name not in existing_groups:
        config.add_memory_group(group_name, members=[])
    config.set_group_permissions(group_name, servers)
    return RedirectResponse(url="/permissions/", status_code=303)


@router.post("/group/delete/{group_name}")
async def delete_group(
    request: Request,
    group_name: str,
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.delete_group_permissions(group_name)
    config.delete_memory_group(group_name)
    return RedirectResponse(url="/permissions/", status_code=303)


@router.post("/group/{group_name}/remove-member")
async def remove_group_member(
    request: Request,
    group_name: str,
    unified_id: str = Form(...),
    _: bool = Depends(require_login),
):
    config = ConfigManager()
    config.remove_user_from_memory_group(group_name, unified_id)
    return RedirectResponse(url="/permissions/", status_code=303)
