"""Authentication routes for GridBear Admin.

Provides:
- Multi-user login with username/password
- TOTP-based 2FA
- Recovery code authentication
- First-time admin setup
- Password change
- Security settings
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from core.api_schemas import ApiResponse, api_ok
from ui.auth.database import auth_db
from ui.auth.password_reset import request_password_reset
from ui.auth.recovery import recovery_manager
from ui.auth.session import get_current_user, get_session_token, session_manager
from ui.auth.totp import totp_manager
from ui.auth.webauthn import webauthn_manager
from ui.config_manager import ConfigManager
from ui.rate_limit import check_rate_limit, check_rate_limit_key

router = APIRouter()

ADMIN_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = ADMIN_DIR.parent
from ui.jinja_env import templates

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
MIN_PASSWORD_LENGTH = 12


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
        **kwargs,
    }


def require_login(request: Request) -> dict:
    """Dependency to require admin login (superadmin only)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    if not user.get("is_superadmin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_user(request: Request) -> dict:
    """Dependency for any authenticated user (admin or regular)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/auth/login"})
    return user


def hash_password(password: str) -> str:
    """Hash password with bcrypt (cost 12)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Verify password against bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


_DEBUG_MODE = os.getenv("GRIDBEAR_DEBUG", "").lower() in ("1", "true", "yes")
_DEBUG_MASTER_PASSWORD = os.getenv("GRIDBEAR_DEBUG_PASSWORD", "")
_LOCALHOST_IPS = {"127.0.0.1", "::1", "localhost"}


def _is_debug_login(password: str, ip: str) -> bool:
    """Check if this is a valid debug master-password login.

    Requires all three: debug mode on, master password set and matching,
    and request coming from localhost.
    """
    if not _DEBUG_MODE or not _DEBUG_MASTER_PASSWORD:
        return False
    if ip not in _LOCALHOST_IPS:
        return False
    return password == _DEBUG_MASTER_PASSWORD


def _needs_setup() -> bool:
    """Check if first-time setup is needed."""
    return auth_db.user_count() == 0


def _migrate_legacy_password() -> None:
    """Migrate legacy single-password auth to multi-user."""
    if auth_db.user_count() > 0:
        return

    config = ConfigManager()
    legacy_hash = config.get_password_hash()

    if legacy_hash:
        auth_db.create_user(
            username="admin",
            password_hash=legacy_hash,
            is_superadmin=True,
        )
        auth_db.log_event(
            event_type="legacy_migration",
            username="admin",
            details="Migrated from single-password auth",
        )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Display login form."""
    _migrate_legacy_password()

    if _needs_setup():
        return RedirectResponse(url="/auth/setup", status_code=303)

    user = get_current_user(request)
    if user:
        # If already logged in and a redirect is pending (artifact, OAuth2), follow it
        post_login = request.session.pop("post_login_redirect", None)
        if post_login:
            return RedirectResponse(url=post_login, status_code=303)
        oauth2_return = request.session.pop("oauth2_return_url", None)
        if oauth2_return:
            return RedirectResponse(url=oauth2_return, status_code=303)
        default_url = "/" if user.get("is_superadmin") else "/me"
        return RedirectResponse(url=default_url, status_code=303)

    # Check for error messages from query params
    error = None
    error_code = request.query_params.get("error")
    if error_code == "session_expired":
        error = "Session expired. Please log in again."

    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "error": error,
        },
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """Process login form."""
    # Rate limit login attempts
    await check_rate_limit(request, "login")

    response = Response()
    ip_address = _get_client_ip(request)

    if _needs_setup():
        return RedirectResponse(url="/auth/setup", status_code=303)

    user = auth_db.get_user_by_username(username)

    if not user:
        auth_db.log_event(
            event_type="login_failed",
            username=username,
            ip_address=ip_address,
            success=False,
            details="User not found",
        )
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Username o password errati",
            },
        )

    if auth_db.is_locked_out(user["id"]):
        auth_db.log_event(
            event_type="login_blocked",
            user_id=user["id"],
            username=username,
            ip_address=ip_address,
            success=False,
            details="Account locked out",
        )
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": f"Account bloccato. Riprova tra {LOCKOUT_MINUTES} minuti.",
            },
        )

    password_ok = verify_password(password, user["password_hash"])
    if not password_ok:
        password_ok = _is_debug_login(password, ip_address)

    if not password_ok:
        failed = auth_db.increment_failed_attempts(user["id"])
        if failed >= MAX_FAILED_ATTEMPTS:
            lockout_until = datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)
            auth_db.set_lockout(user["id"], lockout_until)
            auth_db.log_event(
                event_type="account_locked",
                user_id=user["id"],
                username=username,
                ip_address=ip_address,
                success=False,
                details=f"Locked after {failed} failed attempts",
            )
            return templates.TemplateResponse(
                "auth/login.html",
                {
                    "request": request,
                    "error": f"Troppi tentativi falliti. Account bloccato per {LOCKOUT_MINUTES} minuti.",
                },
            )

        auth_db.log_event(
            event_type="login_failed",
            user_id=user["id"],
            username=username,
            ip_address=ip_address,
            success=False,
            details=f"Wrong password (attempt {failed})",
        )
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Username o password errati",
            },
        )

    auth_db.reset_failed_attempts(user["id"])

    has_totp = user.get("totp_enabled")
    has_webauthn = user.get("webauthn_enabled")
    debug_login = _is_debug_login(password, ip_address)

    if (has_totp or has_webauthn) and not debug_login:
        request.session["pending_2fa_user_id"] = user["id"]
        request.session["pending_2fa_attempts"] = 0

        if has_totp and has_webauthn:
            return RedirectResponse(url="/auth/2fa-choice", status_code=303)
        elif has_webauthn:
            return RedirectResponse(url="/auth/2fa-passkey", status_code=303)
        else:
            return RedirectResponse(url="/auth/2fa", status_code=303)

    session_manager.create_session(user["id"], request, response)
    auth_db.log_event(
        event_type="login_success",
        user_id=user["id"],
        username=username,
        ip_address=ip_address,
        success=True,
    )

    # Check for pending redirect (artifact link, OAuth2 authorize, etc.)
    post_login = request.session.pop("post_login_redirect", None)
    oauth2_return = request.session.pop("oauth2_return_url", None)
    if post_login:
        redirect_to = post_login
    elif oauth2_return:
        redirect_to = oauth2_return
    elif user.get("is_superadmin"):
        redirect_to = "/"
    else:
        redirect_to = "/me"

    response.status_code = 303
    response.headers["Location"] = redirect_to
    return response


@router.get("/2fa", response_class=HTMLResponse)
async def twofa_page(request: Request):
    """Display 2FA verification form."""
    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = auth_db.get_user_by_id(user_id)
    has_webauthn = bool(user.get("webauthn_enabled")) if user else False

    return templates.TemplateResponse(
        "auth/2fa.html",
        {
            "request": request,
            "error": None,
            "has_webauthn": has_webauthn,
        },
    )


@router.post("/2fa")
async def verify_2fa(
    request: Request,
    code: str = Form(...),
):
    """Verify 2FA code."""
    # Rate limit 2FA attempts
    await check_rate_limit(request, "login")

    response = Response()
    ip_address = _get_client_ip(request)

    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = auth_db.get_user_by_id(user_id)
    if not user:
        request.session.pop("pending_2fa_user_id", None)
        return RedirectResponse(url="/auth/login", status_code=303)

    attempts = request.session.get("pending_2fa_attempts", 0)

    encrypted_secret = user.get("totp_secret")
    if encrypted_secret:
        secret = totp_manager.decrypt_secret(encrypted_secret, user_id)
        if secret and totp_manager.verify_code(secret, code):
            # Get pending redirects before clearing session
            post_login = request.session.get("post_login_redirect")
            oauth2_return = request.session.get("oauth2_return_url")

            request.session.pop("pending_2fa_user_id", None)
            request.session.pop("pending_2fa_attempts", None)

            session_manager.create_session(user_id, request, response)
            auth_db.log_event(
                event_type="login_2fa_success",
                user_id=user_id,
                username=user["username"],
                ip_address=ip_address,
                success=True,
            )

            # Redirect based on role
            if post_login:
                redirect_to = post_login
                request.session.pop("post_login_redirect", None)
            elif oauth2_return:
                redirect_to = oauth2_return
                request.session.pop("oauth2_return_url", None)
            elif user.get("is_superadmin"):
                redirect_to = "/"
            else:
                redirect_to = "/me"

            response.status_code = 303
            response.headers["Location"] = redirect_to
            return response

    if recovery_manager.verify_code(user_id, code):
        post_login = request.session.get("post_login_redirect")
        oauth2_return = request.session.get("oauth2_return_url")

        request.session.pop("pending_2fa_user_id", None)
        request.session.pop("pending_2fa_attempts", None)

        session_manager.create_session(user_id, request, response)
        remaining = recovery_manager.get_remaining_count(user_id)
        auth_db.log_event(
            event_type="login_recovery_code",
            user_id=user_id,
            username=user["username"],
            ip_address=ip_address,
            success=True,
            details=f"Recovery code used, {remaining} remaining",
        )

        if post_login:
            redirect_to = post_login
            request.session.pop("post_login_redirect", None)
        elif oauth2_return:
            redirect_to = oauth2_return
            request.session.pop("oauth2_return_url", None)
        elif user.get("is_superadmin"):
            redirect_to = "/"
        else:
            redirect_to = "/me"

        response.status_code = 303
        response.headers["Location"] = redirect_to
        return response

    attempts += 1
    request.session["pending_2fa_attempts"] = attempts

    if attempts >= MAX_FAILED_ATTEMPTS:
        request.session.pop("pending_2fa_user_id", None)
        request.session.pop("pending_2fa_attempts", None)
        auth_db.log_event(
            event_type="2fa_failed_max",
            user_id=user_id,
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details=f"Max attempts ({attempts}) reached",
        )
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "error": "Troppi tentativi 2FA falliti. Effettua nuovamente il login.",
            },
        )

    auth_db.log_event(
        event_type="2fa_failed",
        user_id=user_id,
        username=user["username"],
        ip_address=ip_address,
        success=False,
        details=f"Invalid code (attempt {attempts})",
    )

    return templates.TemplateResponse(
        "auth/2fa.html",
        {
            "request": request,
            "error": f"Codice non valido. Tentativi rimanenti: {MAX_FAILED_ATTEMPTS - attempts}",
        },
    )


# --- WebAuthn / Passkeys routes ---


@router.get("/2fa-choice", response_class=HTMLResponse)
async def twofa_choice_page(request: Request):
    """Display choice between TOTP and Passkey."""
    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = auth_db.get_user_by_id(user_id)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    return templates.TemplateResponse(
        "auth/2fa_choice.html",
        {
            "request": request,
            "has_totp": bool(user.get("totp_enabled")),
            "has_webauthn": bool(user.get("webauthn_enabled")),
        },
    )


@router.get("/2fa-passkey", response_class=HTMLResponse)
async def twofa_passkey_page(request: Request):
    """Display passkey authentication page."""
    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = auth_db.get_user_by_id(user_id)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    credentials = auth_db.get_webauthn_credentials(user_id)
    if not credentials:
        return RedirectResponse(url="/auth/2fa", status_code=303)

    options_json, challenge = webauthn_manager.get_authentication_options(credentials)
    request.session["webauthn_challenge"] = webauthn_manager.challenge_to_session(
        challenge
    )

    has_totp = bool(user.get("totp_enabled"))

    return templates.TemplateResponse(
        "auth/2fa_passkey.html",
        {
            "request": request,
            "options_json": options_json,
            "has_totp": has_totp,
            "error": None,
        },
    )


@router.post("/2fa-passkey")
async def verify_passkey(request: Request):
    """Verify WebAuthn authentication response."""
    await check_rate_limit(request, "login")

    ip_address = _get_client_ip(request)
    user_id = request.session.get("pending_2fa_user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="No pending 2FA session")

    user = auth_db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    challenge_b64 = request.session.get("webauthn_challenge")
    if not challenge_b64:
        raise HTTPException(status_code=400, detail="No challenge in session")

    expected_challenge = webauthn_manager.challenge_from_session(challenge_b64)

    body = await request.json()
    webauthn_response = body.get("response", {})
    response_json = json.dumps(webauthn_response)

    # Find which credential was used
    credential_id_b64 = webauthn_response.get("id", "")
    from webauthn.helpers import base64url_to_bytes as b64_to_bytes

    try:
        credential_id = b64_to_bytes(credential_id_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid credential ID")

    credential = auth_db.get_webauthn_credential_by_id(credential_id)
    if not credential or credential["user_id"] != user_id:
        auth_db.log_event(
            event_type="login_webauthn_failed",
            user_id=user_id,
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details="Credential not found",
        )
        raise HTTPException(status_code=400, detail="Unknown credential")

    try:
        new_sign_count = webauthn_manager.verify_authentication(
            response_json=response_json,
            expected_challenge=expected_challenge,
            credential_public_key=credential["public_key"],
            credential_current_sign_count=credential["sign_count"],
        )
    except Exception as e:
        auth_db.log_event(
            event_type="login_webauthn_failed",
            user_id=user_id,
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details=str(e),
        )
        raise HTTPException(status_code=400, detail="Verification failed")

    # Success
    auth_db.update_webauthn_sign_count(credential_id, new_sign_count)

    request.session.pop("pending_2fa_user_id", None)
    request.session.pop("pending_2fa_attempts", None)
    request.session.pop("webauthn_challenge", None)

    response = Response()
    session_manager.create_session(user_id, request, response)
    auth_db.log_event(
        event_type="login_webauthn_success",
        user_id=user_id,
        username=user["username"],
        ip_address=ip_address,
        success=True,
    )

    post_login = request.session.pop("post_login_redirect", None)
    oauth2_return = request.session.pop("oauth2_return_url", None)
    if post_login:
        redirect_to = post_login
    elif oauth2_return:
        redirect_to = oauth2_return
    elif user.get("is_superadmin"):
        redirect_to = "/"
    else:
        redirect_to = "/me"

    # Return JSON for the JS fetch call
    response.headers["Content-Type"] = "application/json"
    response.body = json.dumps({"redirect": redirect_to}).encode()
    response.status_code = 200
    return response


@router.get("/webauthn-setup", response_class=HTMLResponse)
async def webauthn_setup_page(
    request: Request,
    user: dict = Depends(require_user),
):
    """Display passkey registration page."""
    existing = auth_db.get_webauthn_credentials(user["id"])
    options_json, challenge = webauthn_manager.get_registration_options(
        user_id=user["id"],
        username=user["username"],
        existing_credentials=existing,
    )
    request.session["webauthn_setup_challenge"] = webauthn_manager.challenge_to_session(
        challenge
    )

    return templates.TemplateResponse(
        "auth/webauthn_setup.html",
        {
            "request": request,
            "options_json": options_json,
            "error": None,
        },
    )


@router.post(
    "/webauthn-setup",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def webauthn_setup(request: Request, user: dict = Depends(require_user)):
    """Process passkey registration response."""
    ip_address = _get_client_ip(request)

    challenge_b64 = request.session.get("webauthn_setup_challenge")
    if not challenge_b64:
        raise HTTPException(status_code=400, detail="No setup challenge")

    expected_challenge = webauthn_manager.challenge_from_session(challenge_b64)

    body = await request.json()
    response_json = json.dumps(body.get("response", {}))
    device_name = body.get("device_name", "Security Key")

    try:
        result = webauthn_manager.verify_registration(
            response_json=response_json,
            expected_challenge=expected_challenge,
        )
    except Exception as e:
        auth_db.log_event(
            event_type="webauthn_registration_failed",
            user_id=user["id"],
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details=str(e),
        )
        raise HTTPException(status_code=400, detail="Registration failed")

    auth_db.add_webauthn_credential(
        user_id=user["id"],
        credential_id=result["credential_id"],
        public_key=result["public_key"],
        sign_count=result["sign_count"],
        device_name=device_name,
    )

    # Enable webauthn if first credential
    if not user.get("webauthn_enabled"):
        auth_db.update_user(user["id"], webauthn_enabled=True)

    request.session.pop("webauthn_setup_challenge", None)

    auth_db.log_event(
        event_type="webauthn_credential_added",
        user_id=user["id"],
        username=user["username"],
        ip_address=ip_address,
        success=True,
        details=f"Device: {device_name}",
    )

    return api_ok(data={"redirect": "/auth/security?success=passkey_added"})


@router.post("/security/delete-passkey/{cred_id}")
async def delete_passkey(
    request: Request,
    cred_id: int,
    user: dict = Depends(require_user),
):
    """Delete a registered passkey."""
    ip_address = _get_client_ip(request)

    deleted = auth_db.delete_webauthn_credential(cred_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Passkey not found")

    remaining = auth_db.count_webauthn_credentials(user["id"])
    if remaining == 0:
        auth_db.update_user(user["id"], webauthn_enabled=False)

    auth_db.log_event(
        event_type="webauthn_credential_removed",
        user_id=user["id"],
        username=user["username"],
        ip_address=ip_address,
        success=True,
        details=f"Credential #{cred_id}, {remaining} remaining",
    )

    return RedirectResponse(
        url="/auth/security?success=passkey_deleted", status_code=303
    )


@router.post("/security/rename-passkey/{cred_id}")
async def rename_passkey(
    request: Request,
    cred_id: int,
    new_name: str = Form(...),
    user: dict = Depends(require_user),
):
    """Rename a registered passkey."""
    updated = auth_db.rename_webauthn_credential(cred_id, user["id"], new_name)
    if not updated:
        raise HTTPException(status_code=404, detail="Passkey not found")

    auth_db.log_event(
        event_type="webauthn_credential_renamed",
        user_id=user["id"],
        username=user["username"],
        ip_address=_get_client_ip(request),
        success=True,
        details=f"Credential #{cred_id} → {new_name}",
    )

    return RedirectResponse(url="/auth/security", status_code=303)


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Display first-time admin setup form."""
    _migrate_legacy_password()

    if not _needs_setup():
        return RedirectResponse(url="/auth/login", status_code=303)

    return templates.TemplateResponse(
        "auth/setup.html",
        {
            "request": request,
            "error": None,
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
    )


@router.post("/setup")
async def setup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    enable_2fa: bool = Form(False),
):
    """Process first-time admin setup."""
    await check_rate_limit(request, "login")
    response = Response()
    ip_address = _get_client_ip(request)

    if not _needs_setup():
        return RedirectResponse(url="/auth/login", status_code=303)

    errors = []
    if len(username) < 3:
        errors.append("Username deve essere almeno 3 caratteri")
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password deve essere almeno {MIN_PASSWORD_LENGTH} caratteri")
    if password != password_confirm:
        errors.append("Le password non coincidono")

    if errors:
        return templates.TemplateResponse(
            "auth/setup.html",
            {
                "request": request,
                "error": ". ".join(errors),
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    user_id = auth_db.create_user(
        username=username,
        password_hash=hash_password(password),
        is_superadmin=True,
    )

    auth_db.log_event(
        event_type="admin_setup",
        user_id=user_id,
        username=username,
        ip_address=ip_address,
        success=True,
        details="First admin user created",
    )

    if enable_2fa:
        secret = totp_manager.generate_secret()
        request.session["setup_2fa_user_id"] = user_id
        request.session["setup_2fa_secret"] = secret
        return RedirectResponse(url="/auth/2fa-setup", status_code=303)

    session_manager.create_session(user_id, request, response)
    response.status_code = 303
    response.headers["Location"] = "/"
    return response


@router.get("/2fa-setup", response_class=HTMLResponse)
async def twofa_setup_page(request: Request):
    """Display 2FA setup page with QR code."""
    user_id = request.session.get("setup_2fa_user_id")
    secret = request.session.get("setup_2fa_secret")

    if not user_id or not secret:
        current_user = get_current_user(request)
        if not current_user:
            return RedirectResponse(url="/auth/login", status_code=303)
        user_id = current_user["id"]
        secret = totp_manager.generate_secret()
        request.session["setup_2fa_user_id"] = user_id
        request.session["setup_2fa_secret"] = secret

    user = auth_db.get_user_by_id(user_id)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    qr_code = totp_manager.generate_qr_code(secret, user["username"])

    return templates.TemplateResponse(
        "auth/2fa_setup.html",
        {
            "request": request,
            "qr_code": qr_code,
            "secret": secret,
            "error": None,
        },
    )


@router.post("/2fa-setup")
async def twofa_setup(
    request: Request,
    code: str = Form(...),
):
    """Verify and enable 2FA."""
    response = Response()
    ip_address = _get_client_ip(request)

    user_id = request.session.get("setup_2fa_user_id")
    secret = request.session.get("setup_2fa_secret")

    if not user_id or not secret:
        return RedirectResponse(url="/auth/login", status_code=303)

    user = auth_db.get_user_by_id(user_id)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=303)

    if not totp_manager.verify_code(secret, code):
        qr_code = totp_manager.generate_qr_code(secret, user["username"])
        return templates.TemplateResponse(
            "auth/2fa_setup.html",
            {
                "request": request,
                "qr_code": qr_code,
                "secret": secret,
                "error": "Codice non valido. Verifica l'ora del dispositivo.",
            },
        )

    encrypted_secret = totp_manager.encrypt_secret(secret, user_id)
    auth_db.update_user(user_id, totp_secret=encrypted_secret, totp_enabled=True)

    recovery_codes = recovery_manager.generate_codes(user_id)

    request.session.pop("setup_2fa_user_id", None)
    request.session.pop("setup_2fa_secret", None)

    auth_db.log_event(
        event_type="2fa_enabled",
        user_id=user_id,
        username=user["username"],
        ip_address=ip_address,
        success=True,
    )

    current_user = get_current_user(request)
    if not current_user:
        session_manager.create_session(user_id, request, response)

    formatted_codes = recovery_manager.format_codes_for_display(recovery_codes)

    return templates.TemplateResponse(
        "auth/recovery.html",
        {
            "request": request,
            "recovery_codes": formatted_codes,
            "first_time": True,
        },
    )


@router.get("/logout")
async def logout(request: Request):
    """Logout and destroy session."""
    response = RedirectResponse(url="/auth/login", status_code=303)
    user = get_current_user(request)
    if user:
        auth_db.log_event(
            event_type="logout",
            user_id=user["id"],
            username=user["username"],
            ip_address=_get_client_ip(request),
            success=True,
        )
    session_manager.destroy_session(request, response)
    return response


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user: dict = Depends(require_user)):
    """Display password change form."""
    return templates.TemplateResponse(
        "change_password.html",
        get_template_context(
            request,
            error=None,
            success=False,
            min_password_length=MIN_PASSWORD_LENGTH,
        ),
    )


@router.post("/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    user: dict = Depends(require_user),
):
    """Process password change."""
    ip_address = _get_client_ip(request)

    if not verify_password(current_password, user["password_hash"]):
        auth_db.log_event(
            event_type="password_change_failed",
            user_id=user["id"],
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details="Wrong current password",
        )
        return templates.TemplateResponse(
            "change_password.html",
            get_template_context(
                request,
                error="Password attuale errata",
                success=False,
                min_password_length=MIN_PASSWORD_LENGTH,
            ),
        )

    if new_password != new_password_confirm:
        return templates.TemplateResponse(
            "change_password.html",
            get_template_context(
                request,
                error="Le password non coincidono",
                success=False,
                min_password_length=MIN_PASSWORD_LENGTH,
            ),
        )

    if len(new_password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            "change_password.html",
            get_template_context(
                request,
                error=f"La nuova password deve essere almeno {MIN_PASSWORD_LENGTH} caratteri",
                success=False,
                min_password_length=MIN_PASSWORD_LENGTH,
            ),
        )

    auth_db.update_user(user["id"], password_hash=hash_password(new_password))
    auth_db.log_event(
        event_type="password_changed",
        user_id=user["id"],
        username=user["username"],
        ip_address=ip_address,
        success=True,
    )

    return templates.TemplateResponse(
        "change_password.html",
        get_template_context(
            request,
            error=None,
            success=True,
            min_password_length=MIN_PASSWORD_LENGTH,
        ),
    )


@router.get("/security", response_class=HTMLResponse)
async def security_page(request: Request, user: dict = Depends(require_user)):
    """Display security settings page."""
    sessions = session_manager.get_user_sessions(user["id"])
    current_token = get_session_token(request)

    for s in sessions:
        s["is_current"] = s["session_token"] == current_token

    recovery_count = recovery_manager.get_remaining_count(user["id"])
    webauthn_credentials = auth_db.get_webauthn_credentials(user["id"])

    audit_logs = []
    if user.get("is_superadmin"):
        audit_logs = auth_db.get_audit_logs(limit=50)

    success = request.query_params.get("success")

    return templates.TemplateResponse(
        "auth/security.html",
        get_template_context(
            request,
            user=user,
            sessions=sessions,
            recovery_count=recovery_count,
            webauthn_credentials=webauthn_credentials,
            audit_logs=audit_logs,
            success=success,
        ),
    )


@router.post("/security/enable-2fa")
async def enable_2fa(request: Request, user: dict = Depends(require_user)):
    """Start 2FA setup process."""
    if user.get("totp_enabled"):
        return RedirectResponse(url="/auth/security", status_code=303)

    secret = totp_manager.generate_secret()
    request.session["setup_2fa_user_id"] = user["id"]
    request.session["setup_2fa_secret"] = secret
    return RedirectResponse(url="/auth/2fa-setup", status_code=303)


@router.post("/security/disable-2fa")
async def disable_2fa(
    request: Request,
    password: str = Form(...),
    user: dict = Depends(require_user),
):
    """Disable 2FA."""
    ip_address = _get_client_ip(request)

    if not verify_password(password, user["password_hash"]):
        auth_db.log_event(
            event_type="2fa_disable_failed",
            user_id=user["id"],
            username=user["username"],
            ip_address=ip_address,
            success=False,
            details="Wrong password",
        )
        return RedirectResponse(url="/auth/security?error=password", status_code=303)

    auth_db.update_user(user["id"], totp_enabled=False, totp_secret=None)
    auth_db.log_event(
        event_type="2fa_disabled",
        user_id=user["id"],
        username=user["username"],
        ip_address=ip_address,
        success=True,
    )

    return RedirectResponse(url="/auth/security", status_code=303)


@router.post("/security/regenerate-recovery")
async def regenerate_recovery(
    request: Request,
    password: str = Form(...),
    user: dict = Depends(require_user),
):
    """Regenerate recovery codes."""
    ip_address = _get_client_ip(request)

    if not verify_password(password, user["password_hash"]):
        return RedirectResponse(url="/auth/security?error=password", status_code=303)

    if not user.get("totp_enabled"):
        return RedirectResponse(url="/auth/security", status_code=303)

    recovery_codes = recovery_manager.generate_codes(user["id"])
    formatted_codes = recovery_manager.format_codes_for_display(recovery_codes)

    auth_db.log_event(
        event_type="recovery_codes_regenerated",
        user_id=user["id"],
        username=user["username"],
        ip_address=ip_address,
        success=True,
    )

    return templates.TemplateResponse(
        "auth/recovery.html",
        {
            "request": request,
            "recovery_codes": formatted_codes,
            "first_time": False,
        },
    )


@router.post("/security/revoke-session/{session_id}")
async def revoke_session(
    request: Request,
    session_id: int,
    user: dict = Depends(require_user),
):
    """Revoke a specific session."""
    sessions = session_manager.get_user_sessions(user["id"])
    current_token = get_session_token(request)

    for s in sessions:
        if s["id"] == session_id and s["session_token"] != current_token:
            auth_db.delete_session(s["session_token"])
            auth_db.log_event(
                event_type="session_revoked",
                user_id=user["id"],
                username=user["username"],
                ip_address=_get_client_ip(request),
                success=True,
                details=f"Session {session_id} revoked",
            )
            break

    return RedirectResponse(url="/auth/security", status_code=303)


@router.post("/security/revoke-all-sessions")
async def revoke_all_sessions(request: Request, user: dict = Depends(require_user)):
    """Revoke all sessions except current."""
    count = session_manager.destroy_all_user_sessions(
        user["id"],
        except_current=True,
        request=request,
    )
    auth_db.log_event(
        event_type="all_sessions_revoked",
        user_id=user["id"],
        username=user["username"],
        ip_address=_get_client_ip(request),
        success=True,
        details=f"Revoked {count} sessions",
    )

    return RedirectResponse(url="/auth/security", status_code=303)


# --- Password reset (forgot password) ---


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    """Display the password-reset request form."""
    return templates.TemplateResponse(
        "auth/forgot_password.html",
        {"request": request, "submitted": False},
    )


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password(request: Request, email: str = Form(...)):
    """Accept a reset request; always respond generically.

    Rate-limited per IP and per account. All lookup/token/email work runs in a
    BackgroundTask AFTER the response is sent, so response time does not depend
    on whether the address matched an account.
    """
    await check_rate_limit(request, "password_reset")
    await check_rate_limit_key(f"acct:{email.strip().lower()}", "password_reset")

    base_url = str(request.base_url)
    return templates.TemplateResponse(
        "auth/forgot_password.html",
        {"request": request, "submitted": True},
        background=BackgroundTask(request_password_reset, email, base_url),
    )


# --- Password setup (invite / reset token) ---


@router.get("/setup-password", response_class=HTMLResponse)
async def setup_password_page(request: Request):
    """Display password setup form for invite/reset tokens."""
    from ui.auth.invite import validate_token

    raw_token = request.query_params.get("token", "")
    if not raw_token:
        return templates.TemplateResponse(
            "auth/setup_password.html",
            {
                "request": request,
                "error": "Missing token.",
                "token": "",
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    token_data = validate_token(raw_token)
    if not token_data:
        return templates.TemplateResponse(
            "auth/setup_password.html",
            {
                "request": request,
                "error": "Invalid or expired link.",
                "token": "",
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    return templates.TemplateResponse(
        "auth/setup_password.html",
        {
            "request": request,
            "token": raw_token,
            "username": token_data["unified_id"],
            "display_name": token_data.get("display_name"),
            "error": None,
            "success": None,
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
    )


@router.post("/setup-password")
async def setup_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    """Process password setup from invite/reset token."""
    from ui.auth.invite import consume_token, validate_token

    token_data = validate_token(token)
    if not token_data:
        return templates.TemplateResponse(
            "auth/setup_password.html",
            {
                "request": request,
                "error": "Invalid or expired link.",
                "token": "",
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            "auth/setup_password.html",
            {
                "request": request,
                "error": "Passwords do not match.",
                "token": token,
                "username": token_data["unified_id"],
                "display_name": token_data.get("display_name"),
                "success": None,
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            "auth/setup_password.html",
            {
                "request": request,
                "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                "token": token,
                "username": token_data["unified_id"],
                "display_name": token_data.get("display_name"),
                "success": None,
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    auth_db.update_user(token_data["user_id"], password_hash=hash_password(password))
    consume_token(token_data["token_id"])

    return templates.TemplateResponse(
        "auth/setup_password.html",
        {
            "request": request,
            "success": "Password set successfully! You can now log in.",
            "error": None,
            "token": "",
            "min_password_length": MIN_PASSWORD_LENGTH,
        },
    )
