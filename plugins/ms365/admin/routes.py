"""Microsoft 365 plugin admin routes."""

import json
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.logging_config import logger
from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.plugin_helpers import load_plugin_config, save_plugin_config
from ui.routes.auth import require_login
from ui.routes.plugins import get_plugin_info, get_template_context
from ui.secrets_manager import secrets_manager

router = APIRouter(prefix="/plugins/ms365", tags=["ms365"])

_MS365_DEFAULTS = {
    "client_id": "",
    "client_secret_env": "MS365_CLIENT_SECRET",
    "redirect_uri": "",
    "database_path": "data/ms365_tokens.db",
    "encryption_key_env": "MS365_ENCRYPTION_KEY",
    "health_check_interval": 300,
    "tenants": [],
}

# Delegated scopes requested during the consent flow, per tenant role.
# Files.Read.All covers files the user can access but does not own, which is what
# the Graph Shares API needs to read documents shared by someone else; plain
# Files.ReadWrite is limited to the user's own drive and 403s on shared items.
_SCOPES_OWNER = (
    "User.Read Files.ReadWrite Files.Read.All Tasks.ReadWrite "
    "Sites.ReadWrite.All Group.Read.All offline_access"
)
_SCOPES_GUEST = "User.Read Files.ReadWrite.All Tasks.ReadWrite offline_access"


def _scopes_for_role(tenant_role: str) -> str:
    """Return the delegated scopes to request for a tenant role."""
    return _SCOPES_OWNER if tenant_role == "owner" else _SCOPES_GUEST


def _default_redirect_uri() -> str:
    """OAuth callback URL, derived from the deployment's public base URL.

    Must point at the callback this router actually serves and be registered
    verbatim in the Azure app registration: Azure compares it literally and
    rejects the login otherwise.
    """
    base = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or "http://localhost:8088"
    return f"{base}/plugins/ms365/callback"


def get_ms365_config() -> dict:
    """Get MS365-specific configuration."""
    config = {**_MS365_DEFAULTS, **load_plugin_config("ms365")}
    if not config.get("redirect_uri"):
        config["redirect_uri"] = _default_redirect_uri()
    return config


def save_ms365_config(ms365_config: dict) -> None:
    """Save MS365-specific configuration."""
    save_plugin_config("ms365", ms365_config)


@router.get("", response_class=HTMLResponse)
async def ms365_index(request: Request, user: dict = Depends(require_login)):
    """MS365 plugin configuration page."""
    config = get_ms365_config()

    has_client_secret = secrets_manager.get("MS365_CLIENT_SECRET") is not None

    tenants_with_status = []
    for tenant in config.get("tenants", []):
        tenant_copy = dict(tenant)
        token_key = f"ms365_token_{tenant['name']}"
        token_data = secrets_manager.get_plain(token_key)
        if token_data:
            try:
                import json

                data = json.loads(token_data)
                tenant_copy["connected"] = True
                tenant_copy["expires_at"] = data.get("expires_at", "")
                tenant_copy["discovered_tenant_id"] = data.get("azure_tenant_id", "")
            except (json.JSONDecodeError, TypeError):
                tenant_copy["connected"] = False
        else:
            tenant_copy["connected"] = False
        tenants_with_status.append(tenant_copy)

    plugin_info = get_plugin_info("ms365")

    return templates.TemplateResponse(
        "plugins/ms365.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name="ms365",
            encryption_available=secrets_manager.is_available(),
            plugin_dependencies=plugin_info.get("dependencies", {})
            if plugin_info
            else {},
            plugin_dependents=[],
            config=config,
            tenants=tenants_with_status,
            has_client_secret=has_client_secret,
            client_id=config.get("client_id", ""),
            redirect_uri=config.get("redirect_uri", ""),
            health_check_interval=config.get("health_check_interval", 300),
        ),
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    health_check_interval: int = Form(300),
    client_secret: str = Form(""),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Save global settings."""
    validate_csrf_token(request, csrf_token)

    config = get_ms365_config()
    config["client_id"] = client_id.strip()
    config["redirect_uri"] = redirect_uri.strip() or _default_redirect_uri()
    config["health_check_interval"] = health_check_interval
    save_ms365_config(config)

    if client_secret.strip():
        secrets_manager.set("MS365_CLIENT_SECRET", client_secret.strip())

    return RedirectResponse(url="/plugins/ms365?saved=settings", status_code=303)


@router.get("/tenant/add", response_class=HTMLResponse)
async def add_tenant_form(request: Request, user: dict = Depends(require_login)):
    """Show add tenant form."""
    return templates.TemplateResponse(
        "plugins/ms365_tenant.html",
        get_template_context(
            request,
            plugin_name="ms365",
            parent_title="Microsoft 365",
            tenant=None,
            is_new=True,
        ),
    )


@router.get("/tenant/{tenant_id}", response_class=HTMLResponse)
async def edit_tenant_form(
    request: Request,
    tenant_id: str,
    user: dict = Depends(require_login),
):
    """Show edit tenant form."""
    config = get_ms365_config()

    tenant = None
    for t in config.get("tenants", []):
        if t.get("id") == tenant_id:
            tenant = t
            break

    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return templates.TemplateResponse(
        "plugins/ms365_tenant.html",
        get_template_context(
            request,
            plugin_name="ms365",
            parent_title="Microsoft 365",
            tenant=tenant,
            is_new=False,
        ),
    )


@router.post("/tenant")
async def save_tenant(
    request: Request,
    tenant_name: str = Form(...),
    tenant_azure_id: str = Form(""),
    role: str = Form("guest"),
    tenant_id: str = Form(""),
    is_new: str = Form("false"),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Save tenant configuration."""
    validate_csrf_token(request, csrf_token)

    config = get_ms365_config()
    tenants = config.get("tenants", [])

    if not tenant_id.strip():
        tenant_id = tenant_name.lower().replace(" ", "-").replace("_", "-")

    tenant_config = {
        "id": tenant_id,
        "name": tenant_name,
        "azure_id": tenant_azure_id.strip() or "common",
        "role": role,
    }

    if is_new == "true":
        for t in tenants:
            if t.get("id") == tenant_id:
                raise HTTPException(status_code=400, detail="Tenant ID already exists")
        tenants.append(tenant_config)
    else:
        for i, t in enumerate(tenants):
            if t.get("id") == tenant_id:
                tenants[i] = tenant_config
                break

    config["tenants"] = tenants
    save_ms365_config(config)

    return RedirectResponse(url="/plugins/ms365?saved=tenant", status_code=303)


@router.post("/tenant/{tenant_id}/delete")
async def delete_tenant(
    request: Request,
    tenant_id: str,
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Delete a tenant configuration and associated token."""
    validate_csrf_token(request, csrf_token)

    config = get_ms365_config()
    tenants = config.get("tenants", [])

    # Find tenant name before removing (needed for token cleanup)
    tenant_name = None
    for t in tenants:
        if t.get("id") == tenant_id:
            tenant_name = t.get("name")
            break

    config["tenants"] = [t for t in tenants if t.get("id") != tenant_id]
    save_ms365_config(config)

    # Clean up OAuth token from vault
    if tenant_name:
        try:
            from plugins.ms365.provider import MS365Provider

            MS365Provider.delete_token(tenant_name)
        except Exception as e:
            logger.warning("Failed to delete token for tenant %s: %s", tenant_name, e)

    return RedirectResponse(url="/plugins/ms365?deleted=tenant", status_code=303)


# Store OAuth state temporarily (in production use Redis/session store)
_oauth_states: dict[str, dict] = {}


@router.get("/auth/start")
async def start_oauth(
    request: Request,
    tenant_name: str,
    user: dict = Depends(require_login),
):
    """Start OAuth flow for a tenant."""
    config = get_ms365_config()
    client_id = config.get("client_id", "")
    redirect_uri = config.get("redirect_uri", "")

    if not client_id:
        raise HTTPException(status_code=400, detail="Client ID not configured")

    azure_authority = "common"
    tenant_role = "guest"
    for tenant in config.get("tenants", []):
        if tenant.get("name") == tenant_name:
            tenant_azure_id = tenant.get("azure_id", "common")
            tenant_role = tenant.get("role", "guest")
            if tenant_azure_id and tenant_azure_id not in ("common", "auto", ""):
                azure_authority = tenant_azure_id
            break

    state = secrets.token_urlsafe(32)

    scopes = _scopes_for_role(tenant_role)

    _oauth_states[state] = {
        "tenant_name": tenant_name,
        "azure_authority": azure_authority,
        "tenant_role": tenant_role,
        "scopes": scopes,
        "created": datetime.now().isoformat(),
    }

    auth_url = (
        f"https://login.microsoftonline.com/{azure_authority}/oauth2/v2.0/authorize"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scopes}"
        f"&state={state}"
    )

    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
):
    """Handle OAuth callback from Microsoft."""
    if error:
        detail = error_description or error
        logger.error("MS365 OAuth: authorization denied by Azure: %s", detail)
        return RedirectResponse(
            url=f"/plugins/ms365?error={detail[:100]}",
            status_code=303,
        )

    if not code:
        return RedirectResponse(url="/plugins/ms365?error=no_code", status_code=303)

    if not state or state not in _oauth_states:
        return RedirectResponse(
            url="/plugins/ms365?error=invalid_state", status_code=303
        )

    state_data = _oauth_states.pop(state)
    tenant_name = state_data["tenant_name"]
    azure_authority = state_data.get("azure_authority", "common")
    tenant_role = state_data.get("tenant_role", "guest")
    scopes_str = state_data.get(
        "scopes", "User.Read Files.ReadWrite Tasks.ReadWrite offline_access"
    )

    scopes_list = [s for s in scopes_str.split() if s != "offline_access"]

    config = get_ms365_config()
    client_id = config.get("client_id", "")
    client_secret = secrets_manager.get_plain("MS365_CLIENT_SECRET")
    redirect_uri = config.get("redirect_uri", "")

    if not client_secret:
        return RedirectResponse(
            url="/plugins/ms365?error=no_client_secret", status_code=303
        )

    try:
        import msal

        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{azure_authority}",
        )

        result = app.acquire_token_by_authorization_code(
            code=code,
            scopes=scopes_list,
            redirect_uri=redirect_uri,
        )

        if "error" in result:
            error_msg = result.get("error_description", result.get("error", "Unknown"))
            logger.error(
                "MS365 OAuth: token exchange failed (tenant=%s authority=%s "
                "redirect_uri=%s scopes=%s): %s",
                tenant_name,
                azure_authority,
                redirect_uri,
                " ".join(scopes_list),
                error_msg,
            )
            return RedirectResponse(
                url=f"/plugins/ms365?error={error_msg[:100]}", status_code=303
            )

        id_token_claims = result.get("id_token_claims", {})
        azure_tenant_id = id_token_claims.get("tid", "unknown")

        from datetime import timedelta

        expires_in = result.get("expires_in", 3600)
        expires_at = (datetime.now() + timedelta(seconds=expires_in)).isoformat()

        token_data = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_at": expires_at,
            "azure_tenant_id": azure_tenant_id,
            "scopes": scopes_list,
            "role": tenant_role,
        }

        secret_key = f"ms365_token_{tenant_name}"
        secrets_manager.set(secret_key, json.dumps(token_data))

        return RedirectResponse(
            url=f"/plugins/ms365?saved=token&tenant={tenant_name}",
            status_code=303,
        )

    except Exception as e:
        logger.exception("MS365 OAuth: callback failed for tenant=%s", tenant_name)
        return RedirectResponse(
            url=f"/plugins/ms365?error={str(e)[:100]}",
            status_code=303,
        )
