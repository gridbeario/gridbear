"""User portal routes (/me/*).

Provides the personal area for authenticated users:
- Dashboard with quick actions and profile summary
- Profile management (display name, email, locale)
- Connections to external services (Phase 3)
- MCP tool preferences (Phase 5)
- Chat with agents (Phase 6)
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from ui.auth.database import auth_db, get_auth_db
from ui.jinja_env import templates
from ui.routes.auth import require_user

router = APIRouter(prefix="/me", tags=["user-portal"])

ADMIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = ADMIN_DIR.parent


def _get_allowed_users(agent_config: dict) -> set[str]:
    """Extract allowed usernames from all channels in an agent config.

    Collects allowed_users from every channel and normalises them
    (strips leading '@').  An empty set means no users are explicitly
    allowed — superadmins can still access.
    """
    users: set[str] = set()
    channels = agent_config.get("channels") or {}
    for channel_cfg in channels.values():
        if not isinstance(channel_cfg, dict):
            continue
        for entry in channel_cfg.get("allowed_users", []):
            users.add(str(entry).lstrip("@").lower())
    return users


def _get_user_agents(user: dict) -> list[dict]:
    """Get agents available to this user.

    Superadmins see all agents.  Regular users only see agents that
    list their username in at least one channel's allowed_users.
    """
    from core.models.agent_config import AgentConfigRecord

    agents = []
    username = (user.get("username") or "").lower()
    is_superadmin = user.get("is_superadmin", False)

    try:
        records = AgentConfigRecord.search_sync([("is_active", "=", True)])
    except Exception:
        return agents

    for record in sorted(records, key=lambda r: r.get("id", "")):
        agent_config = dict(record)

        # Access check: superadmins see all, others must be listed
        # in at least one channel's allowed_users.  Agents without
        # any allowed_users (e.g. internal-only) are admin-only.
        if not is_superadmin:
            allowed = _get_allowed_users(agent_config)
            if not allowed or username not in allowed:
                continue

        agents.append(
            {
                "name": agent_config.get("id", ""),
                "display_name": agent_config.get("name", agent_config.get("id", "")),
                "description": agent_config.get("description", ""),
                "avatar": agent_config.get("avatar", ""),
            }
        )

    # Sort by last conversation activity (most recent first)
    if agents:
        try:
            from core.registry import get_database

            db = get_database()
            if db:
                uid = username
                with db.acquire_sync() as conn:
                    cur = conn.execute(
                        """SELECT agent_name, MAX(updated_at) AS last_active
                           FROM chat.webchat_conversations
                           WHERE unified_id = %s
                           GROUP BY agent_name""",
                        (uid,),
                    )
                    last_active = {
                        row["agent_name"]: row["last_active"] for row in cur.fetchall()
                    }
                if last_active:
                    from datetime import datetime, timezone

                    epoch = datetime.min.replace(tzinfo=timezone.utc)
                    agents.sort(
                        key=lambda a: last_active.get(a["name"], epoch),
                        reverse=True,
                    )
        except Exception:
            pass

    return agents


def _is_token_expired(token_raw: str) -> bool:
    """Check if an OAuth2 token JSON string is expired."""
    import time

    try:
        token_data = json.loads(token_raw)
        expires_at = token_data.get("expires_at")
        if expires_at is not None and time.time() > float(expires_at):
            return True
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return False


async def _invalidate_mcp_connections(unified_id: str, conn_id: str) -> None:
    """Invalidate cached MCP gateway connections when credentials change.

    Maps the service connection ID (e.g. "odoo") to the MCP server name
    (e.g. "odoo-mcp") and tells the client manager to drop the cached
    SSE/HTTP connection so the next request reconnects with fresh credentials.
    """
    try:
        from core.mcp_gateway.server import get_client_manager

        cm = get_client_manager()
        if not cm:
            return
        # Find which server_name(s) use this service_connection_id
        server_names = [
            name
            for name, info in cm._known_servers.items()
            if info.service_connection_id == conn_id
        ]
        for server_name in server_names:
            await cm.invalidate_user_connections(unified_id, server_name)
        if not server_names:
            # Fallback: invalidate all connections for this user
            await cm.invalidate_user_connections(unified_id)
    except Exception as e:
        logger.warning("Failed to invalidate MCP connections: %s", e)


async def _notify_expired_tokens(connections: list[dict], user: dict) -> None:
    """Create bell notifications for any expired OAuth2 tokens."""
    expired = [c for c in connections if c.get("token_expired")]
    if not expired:
        return
    from ui.services.notifications import NotificationService

    ns = NotificationService.get()
    uid = user.get("username", "")
    for conn in expired:
        await ns.create(
            category="oauth_expired",
            severity="warning",
            title=f"Token scaduto: {conn['name']}",
            message="Il token OAuth2 è scaduto. Rinnova la connessione.",
            source=conn.get("plugin_name"),
            user_id=uid,
            action_url="/me/connections",
        )


async def _try_refresh_expired_tokens(connections: list[dict], user: dict) -> None:
    """Proactively refresh expired OAuth2 tokens using refresh_token.

    Mutates connection dicts in-place: sets token_expired=False on success.
    """
    unified_id = user["username"]
    if not unified_id:
        return

    for conn in connections:
        if not conn.get("token_expired"):
            continue
        if not conn.get("auth_type", "").startswith("oauth2"):
            continue

        plugin_name = conn.get("plugin_name")
        conn_id = conn.get("id")
        if not plugin_name or not conn_id:
            continue

        from ui.secrets_manager import secrets_manager

        cred_key = f"user:{unified_id}:svc:{conn_id}:token"
        token_raw = secrets_manager.get_plain(cred_key)
        if not token_raw:
            continue

        try:
            token_data = json.loads(token_raw)
        except (json.JSONDecodeError, TypeError):
            continue

        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            continue

        # Load provider OAuth config for token_url + client credentials
        try:
            from core.mcp_gateway.provider_loader import _load_provider_class
            from core.registry import get_plugin_path

            plugin_path = get_plugin_path(plugin_name)
            if not plugin_path:
                continue

            manifest_path = plugin_path / "manifest.json"
            if not manifest_path.exists():
                continue

            with open(manifest_path) as f:
                manifest = json.load(f)

            from ui.plugin_helpers import load_plugin_config

            plugin_config = load_plugin_config(plugin_name)

            provider_cls = _load_provider_class(plugin_name, manifest)
            if not provider_cls or not hasattr(provider_cls, "get_oauth_config"):
                continue

            provider = provider_cls(plugin_config)
            oauth_config = provider.get_oauth_config(conn_id)
            if not oauth_config or not oauth_config.get("token_url"):
                continue

            # Standard OAuth2 refresh_token grant
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    oauth_config["token_url"],
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": oauth_config["client_id"],
                        "client_secret": oauth_config.get("client_secret", ""),
                    },
                )
                if resp.status_code == 200:
                    import time

                    new_token = resp.json()
                    new_token["expires_at"] = time.time() + new_token.get(
                        "expires_in", 3600
                    )
                    # Preserve refresh_token if the server didn't return a new one
                    if "refresh_token" not in new_token:
                        new_token["refresh_token"] = refresh_token
                    secrets_manager.set(
                        cred_key,
                        json.dumps(new_token),
                        description=f"User {unified_id} OAuth token for {conn_id}",
                    )
                    conn["token_expired"] = False
                    logger.info(
                        "Proactive OAuth2 refresh succeeded for %s/%s (user %s)",
                        plugin_name,
                        conn_id,
                        unified_id,
                    )
                else:
                    logger.warning(
                        "Proactive OAuth2 refresh failed HTTP %d for %s/%s: %s",
                        resp.status_code,
                        plugin_name,
                        conn_id,
                        resp.text[:200],
                    )
        except Exception as exc:
            logger.warning(
                "Proactive OAuth2 refresh error for %s/%s: %s",
                plugin_name,
                conn_id,
                exc,
            )


def _get_service_connections(user: dict) -> list[dict]:
    """Get available service connections and their status for a user."""
    from core.registry import get_path_resolver
    from ui.plugin_helpers import get_enabled_plugins

    resolver = get_path_resolver()
    connections = []

    unified_id = user["username"]
    all_manifests = resolver.discover_all() if resolver else {}

    for plugin_name in get_enabled_plugins():
        manifest = all_manifests.get(plugin_name)
        if manifest is None:
            continue

        for svc in manifest.get("service_connections", []):
            # Agent-only connections are managed by admin, not user portal
            if svc.get("agent_only"):
                continue

            connect_url = svc.get("connect_url")
            disconnect_url = svc.get("disconnect_url")

            if svc.get("multi_account"):
                # Multi-account service — delegate status check to generic creds
                accounts = _get_multi_accounts(plugin_name, svc, unified_id)
                connections.append(
                    {
                        "id": svc["id"],
                        "name": svc.get("name", svc["id"]),
                        "description": svc.get("description", ""),
                        "auth_type": svc.get("auth_type", "none"),
                        "icon": svc.get("icon", "fas fa-cube"),
                        "color": svc.get("color", "gray"),
                        "plugin_name": plugin_name,
                        "multi_account": True,
                        "connected": bool(accounts),
                        "accounts": accounts,
                        "account_info": None,
                        "connect_url": connect_url,
                        "disconnect_url": disconnect_url,
                    }
                )
            elif connect_url:
                # Plugin provides its own portal — we don't know status
                from ui.secrets_manager import secrets_manager

                cred_key = f"user:{unified_id}:svc:{svc['id']}:token"
                token_raw = secrets_manager.get_plain(cred_key)
                connected = bool(token_raw)
                token_expired = False
                if connected and svc.get("auth_type") == "oauth2_bearer":
                    token_expired = _is_token_expired(token_raw)
                connections.append(
                    {
                        "id": svc["id"],
                        "name": svc.get("name", svc["id"]),
                        "description": svc.get("description", ""),
                        "auth_type": svc.get("auth_type", "none"),
                        "icon": svc.get("icon", "fas fa-cube"),
                        "color": svc.get("color", "gray"),
                        "plugin_name": plugin_name,
                        "multi_account": False,
                        "connected": connected,
                        "token_expired": token_expired,
                        "accounts": [],
                        "account_info": None,
                        "connect_url": connect_url,
                        "disconnect_url": disconnect_url,
                    }
                )
            else:
                # Standard 1:1 service connection
                from ui.secrets_manager import secrets_manager

                cred_key = f"user:{unified_id}:svc:{svc['id']}:token"
                token_raw = secrets_manager.get_plain(cred_key)
                connected = bool(token_raw)
                token_expired = False

                # Check OAuth2 token expiry
                if connected and svc.get("auth_type") == "oauth2_bearer":
                    token_expired = _is_token_expired(token_raw)

                if not connected:
                    for suffix in ("api_key", "credentials"):
                        alt_key = f"user:{unified_id}:svc:{svc['id']}:{suffix}"
                        if secrets_manager.get(alt_key) is not None:
                            connected = True
                            break

                connections.append(
                    {
                        "id": svc["id"],
                        "name": svc.get("name", svc["id"]),
                        "description": svc.get("description", ""),
                        "auth_type": svc.get("auth_type", "none"),
                        "icon": svc.get("icon", "fas fa-cube"),
                        "color": svc.get("color", "gray"),
                        "plugin_name": plugin_name,
                        "multi_account": False,
                        "connected": connected,
                        "token_expired": token_expired,
                        "accounts": [],
                        "account_info": None,
                        "connect_url": connect_url,
                        "disconnect_url": disconnect_url,
                    }
                )

    return connections


def _get_multi_accounts(plugin_name: str, svc: dict, unified_id: str) -> list[dict]:
    """Get actual accounts for a multi-account service.

    Uses config_manager to look up accounts mapped to this user,
    then checks secrets for connection status using the pattern
    {svc_id}_token_{account} (convention for multi-account plugins).
    """
    from ui.config_manager import ConfigManager
    from ui.secrets_manager import secrets_manager

    config_manager = ConfigManager()
    # Convention: get_{svc_id}_accounts() on config_manager
    getter = getattr(config_manager, f"get_{svc['id']}_accounts", None)
    if not getter:
        return []

    all_accounts = getter()
    account_ids = all_accounts.get(unified_id, [])

    return [
        {
            "email": acct,
            "connected": secrets_manager.exists(f"{svc['id']}_token_{acct}"),
        }
        for acct in account_ids
    ]


# --- Dashboard ---


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(require_user)):
    agents = _get_user_agents(user)
    connections = _get_service_connections(user)
    await _try_refresh_expired_tokens(connections, user)
    connected_count = sum(1 for c in connections if c["connected"])
    await _notify_expired_tokens(connections, user)

    return templates.TemplateResponse(
        "me/dashboard.html",
        {
            "request": request,
            "user": user,
            "agents_count": len(agents),
            "connected_count": connected_count,
            "total_connections": len(connections),
        },
    )


# --- Profile ---


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: dict = Depends(require_user)):
    from core.i18n import get_active_languages

    return templates.TemplateResponse(
        "me/profile.html",
        {
            "request": request,
            "user": user,
            "available_languages": get_active_languages(),
            "success": request.query_params.get("success") == "1",
            "error": None,
        },
    )


@router.post("/profile")
async def profile_update(
    request: Request,
    display_name: str = Form(""),
    email: str = Form(""),
    locale: str = Form("en"),
    user: dict = Depends(require_user),
):
    from core.i18n import get_active_languages, get_default_language

    if locale not in get_active_languages():
        locale = get_default_language()

    auth_db.update_user(
        user["id"],
        display_name=display_name.strip() or None,
        email=email.strip() or None,
        locale=locale,
    )

    return RedirectResponse(url="/me/profile?success=1", status_code=303)


AVATARS_DIR = BASE_DIR / "data" / "avatars"


@router.post("/profile/avatar")
async def profile_avatar_upload(
    request: Request,
    avatar: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    """Upload user avatar image."""
    if not avatar.content_type or not avatar.content_type.startswith("image/"):
        return RedirectResponse(url="/me/profile?error=invalid_image", status_code=303)

    # Max 2MB
    contents = await avatar.read()
    if len(contents) > 2 * 1024 * 1024:
        return RedirectResponse(url="/me/profile?error=file_too_large", status_code=303)

    AVATARS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine extension from content type
    ext = "jpg"
    if avatar.content_type == "image/png":
        ext = "png"
    elif avatar.content_type == "image/webp":
        ext = "webp"

    # Remove old avatar files for this user
    for old in AVATARS_DIR.glob(f"{user['id']}.*"):
        old.unlink()

    avatar_path = AVATARS_DIR / f"{user['id']}.{ext}"
    avatar_path.write_bytes(contents)

    avatar_url = f"/me/avatar/{user['id']}.{ext}"
    auth_db.update_user(user["id"], avatar_url=avatar_url)

    return RedirectResponse(url="/me/profile?success=1", status_code=303)


@router.get("/avatar/{filename}")
async def serve_avatar(filename: str):
    """Serve user avatar image."""
    path = (AVATARS_DIR / filename).resolve()
    if not path.is_relative_to(AVATARS_DIR.resolve()):
        return RedirectResponse(url="/static/img/default-avatar.svg", status_code=302)
    if not path.exists() or not path.is_file():
        return RedirectResponse(url="/static/img/default-avatar.svg", status_code=302)
    return FileResponse(path)


# --- Connections ---


@router.get("/connections", response_class=HTMLResponse)
async def connections_page(request: Request, user: dict = Depends(require_user)):
    connections = _get_service_connections(user)
    await _try_refresh_expired_tokens(connections, user)
    await _notify_expired_tokens(connections, user)

    return templates.TemplateResponse(
        "me/connections.html",
        {
            "request": request,
            "user": user,
            "connections": connections,
        },
    )


@router.get("/connections/{plugin_name}/{conn_id}/connect", response_class=HTMLResponse)
async def connection_connect(
    request: Request,
    plugin_name: str,
    conn_id: str,
    user: dict = Depends(require_user),
):
    """Start connection flow (OAuth redirect or credential form)."""
    # Load manifest to get connection info
    from core.registry import get_plugin_path

    plugin_path = get_plugin_path(plugin_name)
    if plugin_path is None:
        return RedirectResponse(url="/me/connections", status_code=303)

    manifest_path = plugin_path / "manifest.json"
    if not manifest_path.exists():
        return RedirectResponse(url="/me/connections", status_code=303)

    with open(manifest_path) as f:
        manifest = json.load(f)

    svc = None
    for s in manifest.get("service_connections", []):
        if s["id"] == conn_id:
            svc = s
            break

    if not svc:
        return RedirectResponse(url="/me/connections", status_code=303)

    auth_type = svc.get("auth_type", "none")

    if auth_type == "api_key":
        return templates.TemplateResponse(
            "me/connect_api_key.html",
            {
                "request": request,
                "user": user,
                "service": svc,
                "plugin_name": plugin_name,
                "error": None,
            },
        )
    elif auth_type == "credentials":
        return templates.TemplateResponse(
            "me/connect_credentials.html",
            {
                "request": request,
                "user": user,
                "service": svc,
                "plugin_name": plugin_name,
                "error": None,
            },
        )
    elif auth_type.startswith("oauth2"):
        # Try to get OAuth config from provider
        try:
            from core.mcp_gateway.provider_loader import _load_provider_class
            from ui.plugin_helpers import load_plugin_config

            plugin_config = load_plugin_config(plugin_name)
            provider_cls = _load_provider_class(plugin_name, manifest)
            if provider_cls and hasattr(provider_cls, "get_oauth_config"):
                provider = provider_cls(plugin_config)
                oauth_config = provider.get_oauth_config(conn_id)
                if oauth_config and oauth_config.get("authorize_url"):
                    import base64
                    import hashlib
                    import secrets

                    state = secrets.token_urlsafe(32)

                    # PKCE: generate code_verifier and code_challenge (S256)
                    code_verifier = secrets.token_urlsafe(43)
                    code_challenge = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(code_verifier.encode()).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )

                    request.session["oauth_state"] = state
                    request.session["oauth_conn"] = f"{plugin_name}:{conn_id}"
                    request.session["oauth_code_verifier"] = code_verifier

                    callback_url = oauth_config.get("redirect_uri") or (
                        os.getenv(
                            "GRIDBEAR_BASE_URL", str(request.base_url).rstrip("/")
                        )
                        + f"/me/connections/{plugin_name}/{conn_id}/callback"
                    )
                    auth_url = (
                        f"{oauth_config['authorize_url']}?"
                        f"client_id={oauth_config['client_id']}&"
                        f"redirect_uri={callback_url}&"
                        f"response_type=code&"
                        f"scope={oauth_config.get('scopes', '')}&"
                        f"state={state}&"
                        f"code_challenge={code_challenge}&"
                        f"code_challenge_method=S256"
                    )
                    return RedirectResponse(url=auth_url, status_code=303)
        except Exception as exc:
            logger.warning(
                "OAuth2 redirect failed for %s/%s: %s",
                plugin_name,
                conn_id,
                exc,
                exc_info=True,
            )

        # Fallback: show a generic token input form
        return templates.TemplateResponse(
            "me/connect_api_key.html",
            {
                "request": request,
                "user": user,
                "service": {
                    **svc,
                    "name": svc.get("name", conn_id) + " (Bearer Token)",
                },
                "plugin_name": plugin_name,
                "error": None,
            },
        )

    return RedirectResponse(url="/me/connections", status_code=303)


@router.post("/connections/{plugin_name}/{conn_id}/connect")
async def connection_connect_post(
    request: Request,
    plugin_name: str,
    conn_id: str,
    user: dict = Depends(require_user),
):
    """Store credentials from connection form."""
    form = await request.form()
    unified_id = user["username"]

    from ui.secrets_manager import secrets_manager

    # Determine what was submitted
    api_key = form.get("api_key", "").strip()
    username = form.get("username", "").strip()
    password = form.get("password", "").strip()

    if api_key:
        secrets_manager.set(
            f"user:{unified_id}:svc:{conn_id}:token",
            api_key,
            description=f"User {unified_id} token for {conn_id}",
        )
    elif username and password:
        secrets_manager.set(
            f"user:{unified_id}:svc:{conn_id}:credentials",
            json.dumps({"username": username, "password": password}),
            description=f"User {unified_id} credentials for {conn_id}",
        )
    else:
        return RedirectResponse(
            url=f"/me/connections/{plugin_name}/{conn_id}/connect?error=missing",
            status_code=303,
        )

    # Invalidate cached connection so gateway uses fresh credentials
    await _invalidate_mcp_connections(unified_id, conn_id)

    return RedirectResponse(url="/me/connections", status_code=303)


@router.get("/connections/{plugin_name}/{conn_id}/callback")
async def connection_oauth_callback(
    request: Request,
    plugin_name: str,
    conn_id: str,
    user: dict = Depends(require_user),
):
    """Handle OAuth callback."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    session_state = request.session.pop("oauth_state", None)
    session_conn = request.session.pop("oauth_conn", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)

    if not code or not state or state != session_state:
        return RedirectResponse(
            url="/me/connections?error=oauth_failed", status_code=303
        )

    if session_conn != f"{plugin_name}:{conn_id}":
        return RedirectResponse(
            url="/me/connections?error=oauth_mismatch", status_code=303
        )

    # Exchange code for token using provider
    from core.registry import get_plugin_path

    plugin_path = get_plugin_path(plugin_name)
    if plugin_path is None:
        return RedirectResponse(
            url="/me/connections?error=plugin_not_found", status_code=303
        )

    manifest_path = plugin_path / "manifest.json"
    if not manifest_path.exists():
        return RedirectResponse(
            url="/me/connections?error=plugin_not_found", status_code=303
        )

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Generic OAuth2 token exchange
    try:
        from core.mcp_gateway.provider_loader import _load_provider_class
        from ui.plugin_helpers import load_plugin_config

        plugin_config = load_plugin_config(plugin_name)
        provider_cls = _load_provider_class(plugin_name, manifest)
        if provider_cls and hasattr(provider_cls, "get_oauth_config"):
            provider = provider_cls(plugin_config)
            oauth_config = provider.get_oauth_config(conn_id)
            if oauth_config and oauth_config.get("token_url"):
                import httpx

                callback_url = (
                    os.getenv("GRIDBEAR_BASE_URL", str(request.base_url).rstrip("/"))
                    + f"/me/connections/{plugin_name}/{conn_id}/callback"
                )
                token_data_req = {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                    "client_id": oauth_config["client_id"],
                    "client_secret": oauth_config.get("client_secret", ""),
                }
                if code_verifier:
                    token_data_req["code_verifier"] = code_verifier

                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        oauth_config["token_url"],
                        data=token_data_req,
                    )
                    if resp.status_code == 200:
                        import time

                        token_data = resp.json()
                        if "expires_in" in token_data:
                            token_data["expires_at"] = (
                                time.time() + token_data["expires_in"]
                            )
                        unified_id = user["username"]
                        from ui.secrets_manager import secrets_manager

                        secrets_manager.set(
                            f"user:{unified_id}:svc:{conn_id}:token",
                            json.dumps(token_data),
                            description=f"User {unified_id} OAuth token for {conn_id}",
                        )
                        # Invalidate cached connection so gateway uses fresh token
                        await _invalidate_mcp_connections(unified_id, conn_id)
                        return RedirectResponse(url="/me/connections", status_code=303)
                    else:
                        logger.warning(
                            "OAuth2 token exchange HTTP %d for %s/%s: %s",
                            resp.status_code,
                            plugin_name,
                            conn_id,
                            resp.text[:500],
                        )
    except Exception as exc:
        logger.warning(
            "OAuth2 token exchange failed for %s/%s: %s",
            plugin_name,
            conn_id,
            exc,
            exc_info=True,
        )

    return RedirectResponse(
        url="/me/connections?error=oauth_exchange_failed", status_code=303
    )


@router.post("/connections/{plugin_name}/{conn_id}/disconnect")
async def connection_disconnect(
    request: Request,
    plugin_name: str,
    conn_id: str,
    user: dict = Depends(require_user),
):
    """Remove stored credentials for a connection."""
    unified_id = user["username"]
    from ui.secrets_manager import secrets_manager

    # Standard 1:1 disconnect
    for suffix in ("token", "api_key", "credentials"):
        key = f"user:{unified_id}:svc:{conn_id}:{suffix}"
        if secrets_manager.get(key) is not None:
            secrets_manager.delete(key)

    # Invalidate cached MCP gateway connections so they don't use stale tokens
    await _invalidate_mcp_connections(unified_id, conn_id)

    return RedirectResponse(url="/me/connections", status_code=303)


# --- Tools ---


@router.get("/tools", response_class=HTMLResponse)
async def tools_page(request: Request, user: dict = Depends(require_user)):
    """Show MCP tool preferences."""
    from core.mcp_gateway.server import get_client_manager

    tool_groups = {}
    client_manager = get_client_manager()
    if client_manager:
        try:
            unified_id = user["username"]
            all_tools = await client_manager.list_all_tools(unified_id=unified_id)
            prefs = get_auth_db().get_user_tool_prefs(unified_id)

            for tool in all_tools:
                full_name = tool["name"]
                # Group by server prefix
                parts = full_name.split("__", 1)
                group = parts[0] if len(parts) > 1 else "other"
                short = parts[1] if len(parts) > 1 else full_name

                if group not in tool_groups:
                    tool_groups[group] = []

                tool_groups[group].append(
                    {
                        "full_name": full_name,
                        "short_name": short,
                        "description": tool.get("description", ""),
                        "enabled": prefs.get(full_name, True),
                    }
                )
        except Exception:
            pass

    return templates.TemplateResponse(
        "me/tools.html",
        {
            "request": request,
            "user": user,
            "tool_groups": tool_groups,
        },
    )


@router.post(
    "/tools/toggle",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def tools_toggle(request: Request, user: dict = Depends(require_user)):
    """Toggle a tool preference."""
    try:
        body = await request.json()
    except Exception:
        return api_error(400, "Invalid JSON", "validation_error")

    tool_name = body.get("tool_name", "")
    enabled = body.get("enabled", True)

    if not tool_name:
        return api_error(400, "Missing tool_name", "validation_error")

    uid = user["username"]
    get_auth_db().set_user_tool_pref(uid, tool_name, enabled)

    return api_ok()


# --- Chat ---


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, user: dict = Depends(require_user)):
    agents = _get_user_agents(user)

    return templates.TemplateResponse(
        "me/chat.html",
        {
            "request": request,
            "user": user,
            "agents": agents,
        },
    )


@router.get("/chat/join/{token}", response_class=HTMLResponse)
async def chat_join_page(
    request: Request, token: str, user: dict = Depends(require_user)
):
    """Show invite confirmation page."""
    from ui.routes.chat_api import _db, _ensure_db

    _ensure_db()
    with _db.acquire_sync() as conn:
        row = conn.execute(
            """SELECT i.conversation_id, i.expires_at, i.max_uses, i.use_count,
                      c.title, c.agent_name
               FROM chat.webchat_invites i
               JOIN chat.webchat_conversations c ON c.id = i.conversation_id
               WHERE i.token = %s AND i.expires_at > NOW()
                 AND (i.max_uses = 0 OR i.use_count < i.max_uses)""",
            (token,),
        ).fetchone()

    if not row:
        return templates.TemplateResponse(
            "me/chat_join.html",
            {"request": request, "user": user, "error": "Invite expired or invalid"},
        )

    return templates.TemplateResponse(
        "me/chat_join.html",
        {
            "request": request,
            "user": user,
            "token": token,
            "conversation_title": row["title"] or "Untitled",
            "agent_name": row["agent_name"],
        },
    )


@router.post("/chat/join/{token}")
async def chat_join_accept(
    request: Request, token: str, user: dict = Depends(require_user)
):
    """Accept an invite and join the conversation."""
    from ui.routes.chat_api import _db, _ensure_db

    uid = user["username"]
    _ensure_db()

    with _db.acquire_sync() as conn:
        # Atomic token redemption
        row = conn.execute(
            """UPDATE chat.webchat_invites
               SET use_count = use_count + 1
               WHERE token = %s
                 AND expires_at > NOW()
                 AND (max_uses = 0 OR use_count < max_uses)
               RETURNING conversation_id""",
            (token,),
        ).fetchone()

        if not row:
            conn.rollback()
            return RedirectResponse(url="/me/chat", status_code=303)

        conv_id = row["conversation_id"]

        # Add as participant (ignore if already member)
        conn.execute(
            """INSERT INTO chat.webchat_participants
               (conversation_id, unified_id, role)
               VALUES (%s, %s, 'member')
               ON CONFLICT DO NOTHING""",
            (conv_id, uid),
        )
        conn.commit()

    return RedirectResponse(url="/me/chat", status_code=303)
