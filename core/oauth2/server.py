"""OAuth2 Authorization Server Endpoints.

Implements RFC 6749 (OAuth2), RFC 7636 (PKCE), RFC 7009 (Revocation),
RFC 7591 (Dynamic Client Registration), RFC 8414 (Server Metadata).
"""

import os
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config.logging_config import logger

from .models import OAuth2Database

router = APIRouter()

ADMIN_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
templates = Jinja2Templates(directory=ADMIN_DIR / "templates")

# CSRF token registered lazily on first use to avoid importing ui.csrf at module level
_csrf_registered = False


def _ensure_csrf_registered():
    global _csrf_registered
    if not _csrf_registered:
        from ui.csrf import get_csrf_token as _get_csrf_token

        templates.env.globals["csrf_token"] = _get_csrf_token
        _csrf_registered = True


# Database instance (initialized lazily)
_db: OAuth2Database | None = None


def get_db() -> OAuth2Database:
    global _db
    if _db is None:
        _db = OAuth2Database()
    return _db


def set_db(db: OAuth2Database):
    global _db
    _db = db


def _json_response(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(
        content=data,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


def _error_response(
    error: str, description: str | None = None, status: int = 400
) -> JSONResponse:
    data = {"error": error}
    if description:
        data["error_description"] = description
    return _json_response(data, status)


def _redirect_error(
    redirect_uri: str,
    error: str,
    description: str | None = None,
    state: str | None = None,
):
    params = {"error": error}
    if description:
        params["error_description"] = description
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302
    )


# ==================== AUTHORIZATION ENDPOINT ====================


@router.get("/authorize", response_class=HTMLResponse)
async def authorize_get(
    request: Request,
    response_type: str = Query(default=None),
    client_id: str = Query(default=None),
    redirect_uri: str = Query(default=None),
    scope: str = Query(default=""),
    state: str = Query(default=None),
    code_challenge: str = Query(default=None),
    code_challenge_method: str = Query(default="S256"),
):
    """OAuth2 Authorization Endpoint (GET - show consent page)."""
    from ui.auth.session import get_current_user

    _ensure_csrf_registered()
    # Validate response_type
    if response_type != "code":
        if redirect_uri:
            return _redirect_error(
                redirect_uri,
                "unsupported_response_type",
                "Only 'code' response_type is supported",
                state,
            )
        return _error_response(
            "unsupported_response_type", "Only 'code' response_type is supported"
        )

    if not redirect_uri:
        return _error_response("invalid_request", "redirect_uri is required")

    if not client_id:
        return _error_response("invalid_request", "client_id is required")

    db = get_db()
    client = db.get_client(client_id)

    if not client:
        return _error_response("invalid_client", "Unknown client_id")

    if not client.validate_redirect_uri(redirect_uri):
        return _error_response(
            "invalid_request",
            f"Invalid redirect_uri. Allowed: {', '.join(client.get_redirect_uris())}",
        )

    if scope and not client.validate_scope(scope):
        return _redirect_error(
            redirect_uri,
            "invalid_scope",
            "Requested scope exceeds allowed scopes",
            state,
        )

    if client.require_pkce and not code_challenge:
        return _redirect_error(
            redirect_uri, "invalid_request", "PKCE code_challenge is required", state
        )

    if code_challenge and code_challenge_method != "S256":
        return _redirect_error(
            redirect_uri,
            "invalid_request",
            "Only 'S256' code_challenge_method is supported",
            state,
        )

    # Check if user is logged in to admin (custom session token)
    user = get_current_user(request)
    admin_user = user["username"] if user else None

    if not admin_user:
        # Store full authorize URL in session so login can redirect back
        params = request.query_params
        request.session["oauth2_return_url"] = (
            f"/oauth2/authorize?{urlencode(dict(params))}"
        )
        return RedirectResponse(url="/auth/login", status_code=302)

    # Show consent page
    return templates.TemplateResponse(
        request,
        "oauth2/consent.html",
        {
            "request": request,
            "client": client,
            "scope": scope,
            "scope_list": scope.split() if scope else [],
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "admin_user": admin_user,
        },
    )


@router.post("/authorize")
async def authorize_post(
    request: Request,
    response_type: str = Form(default=None),
    client_id: str = Form(default=None),
    redirect_uri: str = Form(default=None),
    scope: str = Form(default=""),
    state: str = Form(default=None),
    code_challenge: str = Form(default=None),
    code_challenge_method: str = Form(default="S256"),
    consent: str = Form(default=None),
):
    """OAuth2 Authorization Endpoint (POST - process consent)."""
    from ui.auth.session import get_current_user

    if not redirect_uri:
        return _error_response("invalid_request", "redirect_uri is required")

    if consent != "allow":
        return _redirect_error(
            redirect_uri,
            "access_denied",
            "User denied the authorization request",
            state,
        )

    db = get_db()
    client = db.get_client(client_id)

    if not client:
        return _error_response("invalid_client", "Unknown client_id")

    if not client.validate_redirect_uri(redirect_uri):
        return _error_response("invalid_request", "Invalid redirect_uri")

    # Get user identity from custom session
    user = get_current_user(request)
    admin_user = user["username"] if user else None
    if not admin_user:
        return _error_response("invalid_request", "User session expired")

    # Create authorization code
    auth_code = db.create_authorization_code(
        client_pk=client.id,
        user_identity=admin_user,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
    )

    # Redirect with code
    params = {"code": auth_code.code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302
    )


# ==================== TOKEN ENDPOINT ====================


@router.post("/token")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(default=None),
    code: str = Form(default=None),
    redirect_uri: str = Form(default=None),
    client_id: str = Form(default=None),
    client_secret: str = Form(default=None),
    code_verifier: str = Form(default=None),
    refresh_token: str = Form(default=None),
    scope: str = Form(default=None),
):
    """OAuth2 Token Endpoint."""
    # Rate limit: 10 req/min per IP
    from core.mcp_gateway.server import _rate_limiter

    ip = request.client.host if request.client else "unknown"
    retry_after = _rate_limiter.check(f"token:{ip}", 10, 60.0)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limit_exceeded", "retry_after": round(retry_after)},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    if grant_type == "authorization_code":
        return _handle_authorization_code(
            code, redirect_uri, client_id, client_secret, code_verifier, request
        )
    elif grant_type == "refresh_token":
        return _handle_refresh_token(
            refresh_token, client_id, client_secret, scope, request
        )
    elif grant_type == "client_credentials":
        return _handle_client_credentials(client_id, client_secret, scope, request)
    else:
        return _error_response(
            "unsupported_grant_type",
            "Supported: authorization_code, refresh_token, client_credentials",
        )


def _handle_authorization_code(
    code: str | None,
    redirect_uri: str | None,
    client_id: str | None,
    client_secret: str | None,
    code_verifier: str | None,
    request: Request,
) -> JSONResponse:
    if not code:
        return _error_response("invalid_request", "code is required")
    if not redirect_uri:
        return _error_response("invalid_request", "redirect_uri is required")

    db = get_db()
    auth_code = db.find_authorization_code(code)

    if not auth_code or not auth_code.is_valid():
        return _error_response("invalid_grant", "Invalid or expired authorization code")

    client = db.get_client_by_id(auth_code.client_id)
    if not client:
        return _error_response("invalid_grant", "Client not found")

    if client_id and client_id != client.client_id:
        return _error_response("invalid_client", "client_id mismatch")

    # Verify secret for confidential clients
    if client.client_type == "confidential":
        if not client_secret or not client.verify_secret(client_secret):
            return _error_response(
                "invalid_client", "Invalid client credentials", status=401
            )

    if redirect_uri != auth_code.redirect_uri:
        return _error_response("invalid_grant", "redirect_uri mismatch")

    # Verify PKCE
    if auth_code.code_challenge:
        if not code_verifier:
            return _error_response("invalid_request", "code_verifier is required")
        from .models import OAuth2Client as ClientModel

        if not ClientModel.verify_pkce(
            code_verifier,
            auth_code.code_challenge,
            auth_code.code_challenge_method or "S256",
        ):
            return _error_response("invalid_grant", "Invalid code_verifier")

    # Mark code as used atomically
    if not db.mark_code_used_atomic(auth_code.id):
        return _error_response(
            "invalid_grant", "Authorization code has already been used"
        )

    # Create access token
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    token = db.create_access_token(
        client_pk=client.id,
        user_identity=auth_code.user_identity,
        scope=auth_code.scope,
        access_expiry=client.access_token_expiry,
        refresh_expiry=client.refresh_token_expiry,
        ip_address=ip,
        user_agent=ua,
    )

    response_data = {
        "access_token": token.token,
        "token_type": token.token_type,
        "expires_in": token.expires_in,
    }
    if token.refresh_token:
        response_data["refresh_token"] = token.refresh_token
    if token.scope:
        response_data["scope"] = token.scope

    logger.info(
        f"OAuth2: issued token for user={auth_code.user_identity} client={client.name}"
    )
    return _json_response(response_data)


def _handle_refresh_token(
    refresh_token: str | None,
    client_id: str | None,
    client_secret: str | None,
    scope: str | None,
    request: Request,
) -> JSONResponse:
    if not refresh_token:
        return _error_response("invalid_request", "refresh_token is required")

    db = get_db()
    old_token = db.find_by_refresh_token(refresh_token)

    if not old_token or not old_token.is_refresh_valid():
        return _error_response("invalid_grant", "Invalid or expired refresh token")

    client = db.get_client_by_id(old_token.client_id)
    if not client:
        return _error_response("invalid_grant", "Client not found")

    if client_id and client_id != client.client_id:
        return _error_response("invalid_client", "client_id mismatch")

    if client.client_type == "confidential":
        if not client_secret or not client.verify_secret(client_secret):
            return _error_response(
                "invalid_client", "Invalid client credentials", status=401
            )

    # Validate scope subset
    if scope and old_token.scope:
        original_scopes = set(old_token.scope.split())
        requested_scopes = set(scope.split())
        if not requested_scopes.issubset(original_scopes):
            return _error_response(
                "invalid_scope", "Requested scope exceeds original grant"
            )
    elif not scope:
        scope = old_token.scope

    # Revoke old token
    db.revoke_token(old_token.id)

    # Create new token
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    new_token = db.create_access_token(
        client_pk=client.id,
        user_identity=old_token.user_identity,
        scope=scope,
        access_expiry=client.access_token_expiry,
        refresh_expiry=client.refresh_token_expiry,
        ip_address=ip,
        user_agent=ua,
    )

    response_data = {
        "access_token": new_token.token,
        "token_type": new_token.token_type,
        "expires_in": new_token.expires_in,
    }
    if new_token.refresh_token:
        response_data["refresh_token"] = new_token.refresh_token
    if new_token.scope:
        response_data["scope"] = new_token.scope

    return _json_response(response_data)


def _handle_client_credentials(
    client_id: str | None,
    client_secret: str | None,
    scope: str | None,
    request: Request,
) -> JSONResponse:
    if not client_id:
        return _error_response("invalid_request", "client_id is required")
    if not client_secret:
        return _error_response("invalid_request", "client_secret is required")

    db = get_db()
    client = db.get_client(client_id)

    if not client:
        return _error_response("invalid_client", "Unknown client_id", status=401)

    if not client.verify_secret(client_secret):
        return _error_response(
            "invalid_client", "Invalid client credentials", status=401
        )

    if scope and not client.validate_scope(scope):
        return _error_response(
            "invalid_scope", "Requested scope exceeds allowed scopes"
        )

    # Create access token (no refresh token for client_credentials per RFC 6749)
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")

    token = db.create_access_token(
        client_pk=client.id,
        user_identity=None,
        scope=scope or client.allowed_scopes,
        access_expiry=client.access_token_expiry,
        refresh_expiry=client.refresh_token_expiry,
        ip_address=ip,
        user_agent=ua,
        include_refresh=False,
    )

    response_data = {
        "access_token": token.token,
        "token_type": token.token_type,
        "expires_in": token.expires_in,
    }
    if token.scope:
        response_data["scope"] = token.scope

    logger.info(f"OAuth2: client_credentials token for client={client.name}")
    return _json_response(response_data)


# ==================== USERINFO ENDPOINT ====================


@router.api_route("/userinfo", methods=["GET", "POST"])
async def userinfo_endpoint(request: Request):
    """OAuth2 UserInfo Endpoint."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return _error_response("invalid_token", "Bearer token required", status=401)

    token_string = auth_header[7:]
    db = get_db()
    token, client = db.validate_token(token_string)

    if not token:
        return _error_response("invalid_token", "Invalid or expired token", status=401)

    scopes = set(token.scope.split()) if token.scope else set()
    userinfo = {"sub": token.user_identity or str(token.client_id)}

    if "profile" in scopes or "openid" in scopes:
        userinfo["name"] = token.user_identity

    if "email" in scopes:
        userinfo["email"] = token.user_identity

    return _json_response(userinfo)


# ==================== REVOKE ENDPOINT ====================


@router.post("/revoke")
async def revoke_endpoint(
    request: Request,
    token: str = Form(default=None),
    token_type_hint: str = Form(default=None),
    client_id: str = Form(default=None),
    client_secret: str = Form(default=None),
):
    """OAuth2 Token Revocation (RFC 7009)."""
    if not token:
        return _error_response("invalid_request", "token is required")

    db = get_db()

    # Validate client if provided
    if client_id:
        client = db.get_client(client_id)
        if client and client.client_type == "confidential":
            if not client_secret or not client.verify_secret(client_secret):
                return _error_response(
                    "invalid_client", "Invalid client credentials", status=401
                )

    db.revoke_by_token_string(token)

    # Always return 200 even if token not found (per RFC 7009)
    return _json_response({})


# ==================== DYNAMIC CLIENT REGISTRATION ====================


@router.post("/register")
async def register_client(request: Request):
    """OAuth2 Dynamic Client Registration (RFC 7591)."""
    try:
        body = await request.json()
    except Exception:
        return _error_response("invalid_request", "Invalid JSON body")

    client_name = body.get("client_name", "Dynamic Client")
    redirect_uris = body.get("redirect_uris", [])
    token_endpoint_auth_method = body.get("token_endpoint_auth_method", "none")
    agent_name = body.get("agent_name")

    is_confidential = token_endpoint_auth_method != "none"
    client_type = "public" if not is_confidential else "confidential"

    if redirect_uris:
        # Validate redirect_uris against trusted domains
        from .config import get_gateway_config

        config = get_gateway_config()
        trusted_domains = config.get("trusted_domains", ["claude.ai"])

        all_trusted = True
        for uri in redirect_uris:
            parsed = urlparse(uri)
            if parsed.netloc.lower() not in [d.lower() for d in trusted_domains]:
                all_trusted = False
                break

        active = all_trusted
        redirect_uris_str = (
            "\n".join(redirect_uris)
            if isinstance(redirect_uris, list)
            else redirect_uris
        )
    elif is_confidential:
        # client_credentials flow — no redirect_uris needed
        active = True
        redirect_uris_str = None
    else:
        return _error_response(
            "invalid_redirect_uri",
            "At least one redirect_uri is required for public clients",
            status=400,
        )

    # Determine scopes: include requested scopes + defaults
    requested_scopes = body.get("scope", "")
    default_scopes = {"openid", "profile", "email", "mcp"}
    if requested_scopes:
        default_scopes.update(requested_scopes.split())
    allowed_scopes = " ".join(sorted(default_scopes))

    db = get_db()
    client, plain_secret = db.create_client(
        name=client_name,
        client_type=client_type,
        redirect_uris=redirect_uris_str,
        allowed_scopes=allowed_scopes,
        require_pkce=bool(redirect_uris),
        active=active,
        agent_name=agent_name,
    )

    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/") or str(
        request.base_url
    ).rstrip("/")

    if redirect_uris:
        grant_types = ["authorization_code", "refresh_token"]
        response_types = ["code"]
    else:
        grant_types = ["client_credentials"]
        response_types = []

    response_data = {
        "client_id": client.client_id,
        "client_name": client.name,
        "redirect_uris": client.get_redirect_uris(),
        "token_endpoint_auth_method": "none"
        if client.client_type == "public"
        else "client_secret_post",
        "grant_types": grant_types,
        "response_types": response_types,
        "registration_client_uri": f"{base_url}/oauth2/register/{client.client_id}",
    }

    if plain_secret:
        response_data["client_secret"] = plain_secret

    if not active:
        response_data["_gridbear_status"] = "pending_approval"
        response_data["_gridbear_message"] = (
            "Client registered but requires admin approval. "
            "Contact the GridBear administrator."
        )

    status = 201
    logger.info(
        f"OAuth2: registered client '{client_name}' (active={active}, type={client_type})"
    )
    return _json_response(response_data, status)
