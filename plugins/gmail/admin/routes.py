"""Gmail OAuth admin routes — tokens stored encrypted in secrets.db."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from plugins.gmail.oauth_utils import (
    OAUTH_CREDENTIALS_PATH,
    add_gmail_tool_permission,
    delete_token,
    get_flow,
    has_token,
    store_token,
    trigger_mcp_reload,
)
from ui.config_manager import ConfigManager
from ui.jinja_env import templates
from ui.routes.auth import require_login

router = APIRouter(prefix="/oauth/gmail", tags=["oauth"])
PLUGIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = PLUGIN_DIR.parent.parent


@router.get("", response_class=HTMLResponse)
async def gmail_accounts_page(request: Request, _: bool = Depends(require_login)):
    config = ConfigManager()

    accounts = []
    for unified_id, emails in config.get_gmail_accounts().items():
        for email in emails:
            has_credentials = has_token(email)
            accounts.append(
                {
                    "unified_id": unified_id,
                    "email": email,
                    "has_credentials": has_credentials,
                }
            )

    unified_ids = config.get_all_unified_ids()

    plugin_menus = getattr(request.state, "plugin_menus", [])
    return templates.TemplateResponse(
        request,
        "gmail.html",
        {
            "request": request,
            "plugin_menus": plugin_menus,
            "accounts": accounts,
            "unified_ids": unified_ids,
            "oauth_configured": OAUTH_CREDENTIALS_PATH.exists(),
        },
    )


@router.get("/start/{token}")
async def start_oauth(request: Request, token: str):
    """Start OAuth flow for a user (accessed via link sent by bot)."""
    config = ConfigManager()
    token_data = config.get_oauth_token_data(token)

    if not token_data:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": "Invalid or expired link. Please request a new link from the bot.",
            },
        )

    request.session["oauth_token"] = token

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    redirect_uri = f"{base_url}/oauth/gmail/callback"

    flow = get_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )

    request.session["oauth_state"] = state
    # google-auth-oauthlib ≥1.3 enables PKCE by default — the verifier must
    # survive to the callback, else Google rejects the exchange.
    request.session["oauth_code_verifier"] = getattr(flow, "code_verifier", None)
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def oauth_callback(
    request: Request, code: str = None, error: str = None, state: str = None
):
    """Handle OAuth callback from Google."""
    if error:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": f"Authorization denied: {error}",
            },
        )

    if not code:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": "No authorization code received.",
            },
        )

    stored_state = request.session.get("oauth_state")
    if not state or state != stored_state:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": "Invalid OAuth state. Possible CSRF attack. Please try again.",
            },
        )

    oauth_token = request.session.get("oauth_token")
    if not oauth_token:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": "Invalid OAuth session. Please try again.",
            },
        )

    config = ConfigManager()
    token_data = config.get_oauth_token_data(oauth_token)

    if not token_data:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": "OAuth token expired. Please request a new link.",
            },
        )

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    redirect_uri = f"{base_url}/oauth/gmail/callback"

    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

    code_verifier = request.session.pop("oauth_code_verifier", None)

    try:
        flow = get_flow(redirect_uri)
        if code_verifier:
            flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        credentials = flow.credentials

        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {credentials.token}"},
            )
            user_info = resp.json()
            email = user_info.get("email")

        if not email:
            raise ValueError("Could not get email from Google")

        token_data_to_save = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes),
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        }
        store_token(email, token_data_to_save)

        unified_id = token_data["unified_id"]
        config.add_gmail_account(unified_id, email)

        add_gmail_tool_permission(email)

        trigger_mcp_reload(email)

        config.delete_oauth_token(oauth_token)
        request.session.pop("oauth_token", None)
        request.session.pop("oauth_state", None)

        return templates.TemplateResponse(
            request,
            "oauth_success.html",
            {
                "request": request,
                "email": email,
            },
        )

    except Exception as e:
        return templates.TemplateResponse(
            request,
            "oauth_error.html",
            {
                "request": request,
                "error": f"Error during authorization: {str(e)}",
            },
        )


@router.post("/generate-link")
async def generate_oauth_link(
    request: Request,
    unified_id: str = Form(...),
    _: bool = Depends(require_login),
):
    """Generate OAuth link for a unified identity (admin action)."""
    config = ConfigManager()
    token = config.create_oauth_token(unified_id)

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")
    link = f"{base_url}/oauth/gmail/start/{token}"

    return {"link": link}


@router.post("/remove/{unified_id}/{email:path}")
async def remove_gmail_account(
    request: Request,
    unified_id: str,
    email: str,
    _: bool = Depends(require_login),
):
    """Remove specific Gmail account for a unified identity."""
    config = ConfigManager()
    config.remove_gmail_account(unified_id, email)
    delete_token(email)
    return RedirectResponse(url="/oauth/gmail", status_code=303)


@router.post("/revoke/{email:path}")
async def revoke_gmail_credentials(
    request: Request,
    email: str,
    _: bool = Depends(require_login),
):
    """Revoke credentials for a Gmail account (delete encrypted token)."""
    delete_token(email)
    return RedirectResponse(url="/oauth/gmail", status_code=303)
