"""User portal routes for Gmail OAuth connections."""

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from config.logging_config import logger
from ui.routes.auth import require_user

router = APIRouter(prefix="/me/connections/gmail", tags=["gmail-portal"])


@router.get("/connect")
async def gmail_oauth_start(
    request: Request,
    user: dict = Depends(require_user),
):
    """Start Google OAuth flow for Gmail connection."""
    from ..oauth_utils import get_flow

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    redirect_uri = f"{base_url}/me/connections/gmail/callback"

    flow = get_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )

    request.session["oauth_state"] = state
    request.session["oauth_conn"] = "gmail:gmail"
    # google-auth-oauthlib ≥1.3 enables PKCE by default: flow.code_verifier
    # is generated above and MUST be replayed in the callback's fetch_token
    # exchange — otherwise Google rejects with "Missing code verifier".
    request.session["oauth_code_verifier"] = getattr(flow, "code_verifier", None)
    return RedirectResponse(url=auth_url, status_code=303)


@router.get("/callback")
async def gmail_oauth_callback(
    request: Request,
    user: dict = Depends(require_user),
):
    """Handle Google OAuth callback."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    session_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)
    request.session.pop("oauth_conn", None)

    if not code or not state or state != session_state:
        return RedirectResponse(
            url="/me/connections?error=oauth_failed", status_code=303
        )

    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

    from ..oauth_utils import (
        add_gmail_tool_permission,
        get_flow,
        trigger_mcp_reload,
    )

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    redirect_uri = f"{base_url}/me/connections/gmail/callback"

    try:
        flow = get_flow(redirect_uri)
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Get email from Google userinfo API
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"},
            )
            email = resp.json().get("email")

        if not email:
            return RedirectResponse(
                url="/me/connections?error=no_email", status_code=303
            )

        # Store token (same format as admin flow)
        token_data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes),
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        }
        from ..provider import GmailProvider

        GmailProvider.store_token(email, token_data)

        # Add to config mapping
        from ui.config_manager import ConfigManager

        unified_id = user.get("unified_id") or user.get("username")
        ConfigManager().add_gmail_account(unified_id, email)

        # Add Claude Code tool permission
        add_gmail_tool_permission(email)

        # Trigger MCP provider reload
        trigger_mcp_reload(email)

        return RedirectResponse(url="/me/connections", status_code=303)
    except Exception:
        logger.exception("Gmail OAuth callback failed")
        return RedirectResponse(
            url="/me/connections?error=oauth_exchange_failed", status_code=303
        )


@router.post("/disconnect")
async def gmail_disconnect(
    request: Request,
    user: dict = Depends(require_user),
):
    """Disconnect a Gmail account."""
    form = await request.form()
    email = form.get("email")

    if not email:
        return RedirectResponse(url="/me/connections", status_code=303)

    unified_id = user.get("unified_id") or user.get("username")

    from ..provider import GmailProvider

    GmailProvider.delete_token(email)

    from ui.config_manager import ConfigManager

    ConfigManager().remove_gmail_account(unified_id, email)

    from ..oauth_utils import trigger_mcp_reload

    trigger_mcp_reload(email)

    return RedirectResponse(url="/me/connections", status_code=303)
