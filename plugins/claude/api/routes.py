"""Claude runner API routes — model management and CLI auth."""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets as _secrets
import threading
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from config.logging_config import logger
from core.api_schemas import ApiResponse, api_error, api_ok
from core.internal_api.auth import verify_internal_auth
from core.registry import get_models_registry
from ui.secrets_manager import secrets_manager

router = APIRouter()

# Strip ANSI escape codes from CLI output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── Model management ──────────────────────────────────────────────────


class ModelEntry(BaseModel):
    id: str
    name: str
    api_id: str | None = None


class SetModelsRequest(BaseModel):
    models: list[ModelEntry]


@router.get(
    "/models",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def get_models(_auth: None = Depends(verify_internal_auth)):
    """Return current Claude model list."""
    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    data = registry.get_metadata("claude")
    return api_ok(data=data)


@router.post(
    "/models",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def set_models(
    request: SetModelsRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Update Claude model list (manual edit)."""
    registry = get_models_registry()
    if not registry:
        return api_error(503, "Models registry not initialized", "unavailable")
    models = [m.model_dump() for m in request.models]
    registry.set_models("claude", models, source="manual")
    return api_ok(count=len(models))


# ── CLI Auth ──────────────────────────────────────────────────────────

_CLI_BASE_ENV = {**os.environ, "HOME": "/home/gridbear"}


def _get_cli_env() -> dict:
    """Build env for CLI subprocesses with OAuth token from secrets manager."""
    env = {**_CLI_BASE_ENV}
    oauth_block = _read_credentials_from_secrets()
    if oauth_block:
        refreshed = _refresh_oauth_token(oauth_block)
        if refreshed:
            oauth_block = refreshed
        if oauth_block.get("accessToken"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_block["accessToken"]
    return env


# Keep a reference to the background login process so it survives
# the request lifecycle and can receive the OAuth callback.
_login_proc: asyncio.subprocess.Process | None = None

# ── PKCE OAuth flow (direct, no CLI TUI) ──────────────────────────────
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_OAUTH_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers"
)

# Pending PKCE login state (one at a time)
_pending_pkce: dict | None = None

# ── Secrets manager helpers ──────────────────────────────────────────

_SECRET_KEY = "CLAUDE_CLI_CREDENTIALS"


def _save_credentials_to_secrets(oauth_block: dict) -> None:
    """Store the full claudeAiOauth block in secrets manager."""
    secrets_manager.set(
        _SECRET_KEY,
        json.dumps(oauth_block),
        description="Claude CLI OAuth credentials (full block)",
    )


def _read_credentials_from_secrets() -> dict | None:
    """Read full claudeAiOauth block from secrets manager."""
    raw = secrets_manager.get_plain(_SECRET_KEY, fallback_env=False)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _clear_credentials_from_secrets() -> None:
    """Remove Claude CLI credentials from secrets manager."""
    secrets_manager.delete(_SECRET_KEY)
    # Also clean up legacy key if present
    try:
        secrets_manager.delete("CLAUDE_CLI_TOKEN")
    except Exception:
        pass


# ── Token refresh ────────────────────────────────────────────────────

_refresh_lock = threading.Lock()

# Buffer before actual expiry to avoid edge-case failures (5 minutes)
_REFRESH_BUFFER_MS = 5 * 60 * 1000


def _refresh_oauth_token(oauth_block: dict) -> dict | None:
    """Refresh an expired OAuth token using the refresh token.

    Returns the updated oauth_block on success, None if no refresh was
    needed or if the refresh failed (caller should use existing token).

    Thread-safe: uses a module-level lock with double-check to avoid
    redundant refreshes when multiple processes spawn concurrently.
    """
    expires_at_ms = oauth_block.get("expiresAt", 0)
    # No expiry info (manual token setup with expiresAt=0) — skip
    if not expires_at_ms:
        return None

    now_ms = int(time.time() * 1000)
    if expires_at_ms - now_ms > _REFRESH_BUFFER_MS:
        return None  # Still valid, no refresh needed

    refresh_token = oauth_block.get("refreshToken", "")
    if not refresh_token:
        logger.debug("OAuth token expired but no refreshToken available")
        return None

    with _refresh_lock:
        # Double-check: another thread may have refreshed while we waited
        fresh_block = _read_credentials_from_secrets()
        if fresh_block:
            fresh_expires = fresh_block.get("expiresAt", 0)
            if fresh_expires and (fresh_expires - now_ms > _REFRESH_BUFFER_MS):
                return fresh_block

        # Perform the refresh
        try:
            import httpx

            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _OAUTH_CLIENT_ID,
            }

            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    _OAUTH_TOKEN_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            if resp.status_code != 200:
                logger.warning(
                    "OAuth token refresh failed: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None

            token_data = resp.json()
            new_block = {
                "accessToken": token_data["access_token"],
                "refreshToken": token_data.get("refresh_token", refresh_token),
                "expiresAt": int(
                    (time.time() + token_data.get("expires_in", 3600)) * 1000
                ),
                "scopes": token_data.get("scope", _OAUTH_SCOPES).split(),
            }
            _save_credentials_to_secrets(new_block)
            logger.info("OAuth token refreshed successfully")
            return new_block

        except Exception as e:
            logger.warning("OAuth token refresh error: %s", e)
            return None


@router.get(
    "/auth/status",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def auth_status(_auth: None = Depends(verify_internal_auth)):
    """Check Claude CLI authentication status."""
    from plugins.claude.runner import get_auth_error_info

    status: dict[str, object] = {"loggedIn": False}
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_get_cli_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        try:
            status = json.loads(stdout.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            status = {
                "loggedIn": False,
                "raw_stdout": stdout.decode(errors="replace").strip(),
                "raw_stderr": stderr.decode(errors="replace").strip(),
            }
    except FileNotFoundError:
        status = {"loggedIn": False, "error": "claude CLI not found"}
    except asyncio.TimeoutError:
        status = {"loggedIn": False, "error": "Auth status check timed out"}
    except Exception as e:
        logger.error("Claude auth status error: %s", e)
        status = {"loggedIn": False, "error": str(e)}

    # Check stored credentials for expiry (survives container restart)
    stored = _read_credentials_from_secrets()
    if stored:
        expires_at_ms = stored.get("expiresAt", 0)
        if expires_at_ms and expires_at_ms < time.time() * 1000:
            status["token_expired"] = True
            status["token_error_message"] = "Token expired — re-authenticate to fix."

    # Also check runtime auth error flag (set during message processing)
    auth_error = get_auth_error_info()
    if auth_error:
        status["token_expired"] = True
        status["token_error_message"] = (
            "Token expired or invalid — last failure at runtime. "
            "Re-authenticate to fix."
        )

    return api_ok(data=status)


class TokenRequest(BaseModel):
    token: str


@router.post(
    "/auth/token",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def setup_token(
    request: TokenRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Set up Claude auth by writing credentials directly.

    The `claude setup-token` command requires an interactive TUI which
    doesn't work in a headless container.  Instead we write the token
    to the credentials file and validate it via `claude auth status`.
    """
    token = request.token.strip()
    if not token:
        return api_error(422, "Token is empty", "validation_error")

    try:
        # Build minimal OAuth block for manual token setup
        oauth_block = {
            "accessToken": token,
            "refreshToken": "",
            "expiresAt": 0,
            "scopes": [
                "user:inference",
                "user:mcp_servers",
                "user:profile",
                "user:sessions:claude_code",
            ],
        }
        # Store in secrets manager (source of truth)
        _save_credentials_to_secrets(oauth_block)

        # Verify it works (CLI reads token from CLAUDE_CODE_OAUTH_TOKEN env var)
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "auth",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_get_cli_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        try:
            status = json.loads(stdout.decode())
            if status.get("loggedIn"):
                # Clear any stale auth error flag so UI shows green
                import plugins.claude.runner as runner_mod

                runner_mod._last_auth_error_at = 0.0
                return api_ok()
            return api_error(
                401,
                "Token saved but auth status shows not logged in. "
                "The token may be invalid or expired.",
                "auth_failed",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return api_ok()  # Token saved, could not verify
    except Exception as e:
        logger.error("Claude token setup error: %s", e)
        return api_error(500, str(e), "internal_error")


@router.post(
    "/auth/logout",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def auth_logout(_auth: None = Depends(verify_internal_auth)):
    """Log out of Claude CLI."""
    global _login_proc
    # Kill any background login process
    if _login_proc and _login_proc.returncode is None:
        try:
            _login_proc.kill()
        except ProcessLookupError:
            pass
        _login_proc = None

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "auth",
            "logout",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_get_cli_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        # Clear from secrets manager regardless of CLI exit code
        try:
            _clear_credentials_from_secrets()
        except Exception:
            pass  # Best-effort cleanup
        if proc.returncode == 0:
            return api_ok()
        return api_error(
            500,
            stderr.decode(errors="replace").strip()
            or stdout.decode(errors="replace").strip(),
            "cli_error",
        )
    except FileNotFoundError:
        return api_error(500, "claude CLI not found", "cli_not_found")
    except asyncio.TimeoutError:
        return api_error(504, "Logout timed out", "timeout")
    except Exception as e:
        logger.error("Claude auth logout error: %s", e)
        return api_error(500, str(e), "internal_error")


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@router.post(
    "/auth/login",
    response_model=ApiResponse[dict],
    response_model_exclude_none=True,
)
async def auth_login(_auth: None = Depends(verify_internal_auth)):
    """Start OAuth PKCE login flow — returns the authorization URL.

    Generates a PKCE code_verifier/challenge pair, stores it server-side,
    and returns the OAuth authorization URL.  The user opens the URL in
    their browser, authorizes, and receives a code on the callback page.
    They paste the code into the admin UI which calls ``/auth/code``.
    """
    global _pending_pkce, _login_proc

    # Kill any stale CLI login process from previous attempts
    if _login_proc and _login_proc.returncode is None:
        try:
            _login_proc.kill()
        except ProcessLookupError:
            pass
        _login_proc = None

    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()

    _pending_pkce = {"verifier": verifier, "state": state}

    from urllib.parse import urlencode

    params = urlencode(
        {
            "code": "true",
            "client_id": _OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "scope": _OAUTH_SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    url = f"{_OAUTH_AUTHORIZE_URL}?{params}"

    return api_ok(data={"url": url, "needs_code": True})


class CodeRequest(BaseModel):
    code: str


@router.post(
    "/auth/code",
    response_model=ApiResponse,
    response_model_exclude_none=True,
)
async def auth_submit_code(
    request: CodeRequest,
    _auth: None = Depends(verify_internal_auth),
):
    """Exchange OAuth authorization code for tokens using PKCE.

    After the user authorizes and receives the code from the callback page,
    this endpoint exchanges it at the token endpoint using the stored
    code_verifier, then writes credentials to .credentials.json and
    secrets manager.
    """
    global _pending_pkce

    raw_code = request.code.strip()
    if not raw_code:
        return api_error(422, "Code is empty", "validation_error")

    # The callback page returns "auth_code#state" — split on '#'
    if "#" in raw_code:
        code, _cb_state = raw_code.split("#", 1)
    else:
        code = raw_code
    logger.debug("OAuth code: raw_len=%d code_len=%d", len(raw_code), len(code))

    if not _pending_pkce:
        return api_error(
            400,
            "No login flow pending — click Login first",
            "no_pending_flow",
        )

    verifier = _pending_pkce["verifier"]
    state = _pending_pkce.get("state", "")
    _pending_pkce = None

    try:
        import httpx

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _OAUTH_REDIRECT_URI,
            "client_id": _OAUTH_CLIENT_ID,
            "code_verifier": verifier,
            "state": state,
        }
        logger.debug("OAuth token exchange payload keys: %s", list(payload.keys()))

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _OAUTH_TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )

        if resp.status_code != 200:
            body = resp.text
            logger.error("OAuth token exchange failed: %s %s", resp.status_code, body)
            # Surface server message for debugging
            detail = ""
            try:
                err = resp.json()
                detail = err.get("error", {}).get("message", body)
            except Exception:
                detail = body
            return api_error(
                502,
                f"Token exchange failed ({resp.status_code}): {detail}",
                "token_exchange_failed",
            )

        token_data = resp.json()
        oauth_block = {
            "accessToken": token_data["access_token"],
            "refreshToken": token_data.get("refresh_token", ""),
            "expiresAt": int((time.time() + token_data.get("expires_in", 3600)) * 1000),
            "scopes": token_data.get("scope", _OAUTH_SCOPES).split(),
        }

        # Store in secrets manager only — the process pool syncs
        # to .credentials.json on-demand before each CLI spawn
        _save_credentials_to_secrets(oauth_block)

        # Clear any stale auth error flag
        import plugins.claude.runner as runner_mod

        runner_mod._last_auth_error_at = 0.0

        logger.info("Claude OAuth login completed successfully")
        return api_ok()

    except Exception as e:
        logger.error("Claude OAuth code exchange error: %s", e)
        return api_error(500, str(e), "internal_error")
