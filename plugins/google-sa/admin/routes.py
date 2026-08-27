"""Google Service Account admin routes.

Global and per-agent SA upload/delete.
Vault keys:
  Global:    svc:google-sa:credentials
  Per-agent: agent:{agent}:svc:google-sa:credentials
Format:      {"sa_b64": "<base64-encoded SA JSON>"}
"""

import base64
import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.csrf import validate_csrf_token
from ui.jinja_env import templates
from ui.routes.agents import list_agents
from ui.routes.auth import require_login
from ui.routes.plugins import get_plugin_info, get_template_context
from ui.secrets_manager import secrets_manager

router = APIRouter(prefix="/plugins/google-sa", tags=["google-sa"])

GLOBAL_SA_KEY = "svc:google-sa:credentials"
AGENT_SA_KEY_TPL = "agent:{agent}:svc:google-sa:credentials"


def _get_plugin_metadata() -> dict:
    """Plugin metadata for auto-sidebar."""
    return get_plugin_info("google-sa") or {}


def _get_sa_email_from_b64(sa_b64: str) -> str | None:
    """Extract client_email from base64-encoded SA JSON."""
    if not sa_b64:
        return None
    try:
        sa_json = base64.b64decode(sa_b64).decode()
        return json.loads(sa_json).get("client_email")
    except Exception:
        return None


def _get_global_sa() -> tuple[bool, str | None]:
    """Return (has_sa, email) for the global SA."""
    raw = secrets_manager.get_plain(GLOBAL_SA_KEY)
    if not raw:
        return False, None
    try:
        data = json.loads(raw)
        sa_b64 = data.get("sa_b64", "")
    except (json.JSONDecodeError, TypeError):
        sa_b64 = raw
    return bool(sa_b64), _get_sa_email_from_b64(sa_b64)


def _get_agents_with_google_access() -> list[dict]:
    """Agents that have any google-* MCP permission (or wildcard)."""
    agents = list_agents()
    result = []
    for agent in agents:
        perms = agent.get("mcp_permissions", [])
        has_google = "*" in perms or any(p.startswith("google") for p in perms)
        if not has_google:
            continue

        agent_name = agent["id"]
        vault_key = AGENT_SA_KEY_TPL.format(agent=agent_name)
        raw = secrets_manager.get_plain(vault_key)
        sa_b64 = ""
        if raw:
            try:
                data = json.loads(raw)
                sa_b64 = data.get("sa_b64", "")
            except (json.JSONDecodeError, TypeError):
                sa_b64 = raw
        result.append(
            {
                "id": agent_name,
                "name": agent.get("name", agent_name),
                "has_sa": bool(sa_b64),
                "sa_email": _get_sa_email_from_b64(sa_b64),
            }
        )
    return result


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def google_sa_index(request: Request, user: dict = Depends(require_login)):
    """Google SA management page."""
    plugin_info = get_plugin_info("google-sa")
    has_sa, sa_email = _get_global_sa()

    return templates.TemplateResponse(
        request,
        "plugins/google_sa.html",
        get_template_context(
            request,
            plugin=plugin_info,
            plugin_name="google-sa",
            has_sa=has_sa,
            sa_email=sa_email,
            agent_sas=_get_agents_with_google_access(),
        ),
    )


@router.post("/upload-sa")
async def upload_sa(
    request: Request,
    sa_file: UploadFile = File(...),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Upload global service account JSON file."""
    validate_csrf_token(request, csrf_token)

    content = await sa_file.read()
    sa_json = content.decode("utf-8").strip()

    try:
        parsed = json.loads(sa_json)
    except (json.JSONDecodeError, ValueError):
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_sa", status_code=303
        )
    if "client_email" not in parsed or "private_key" not in parsed:
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_sa", status_code=303
        )

    sa_b64 = base64.b64encode(sa_json.encode()).decode()
    creds = json.dumps({"sa_b64": sa_b64})
    secrets_manager.set(
        GLOBAL_SA_KEY, creds, description="Google service account (global)"
    )

    return RedirectResponse(url="/plugins/google-sa?saved=sa", status_code=303)


@router.post("/delete-sa")
async def delete_sa(
    request: Request,
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Delete global service account from vault."""
    validate_csrf_token(request, csrf_token)
    secrets_manager.delete(GLOBAL_SA_KEY)

    return RedirectResponse(url="/plugins/google-sa?deleted=sa", status_code=303)


@router.post("/agent-sa/upload")
async def upload_agent_sa(
    request: Request,
    agent_name: str = Form(...),
    sa_file: UploadFile = File(...),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Upload SA for a specific agent."""
    validate_csrf_token(request, csrf_token)

    known = {a["id"] for a in _get_agents_with_google_access()}
    if agent_name not in known:
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_agent", status_code=303
        )

    content = await sa_file.read()
    sa_json = content.decode("utf-8").strip()

    try:
        parsed = json.loads(sa_json)
    except (json.JSONDecodeError, ValueError):
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_sa", status_code=303
        )
    if "client_email" not in parsed or "private_key" not in parsed:
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_sa", status_code=303
        )

    sa_b64 = base64.b64encode(sa_json.encode()).decode()
    vault_key = AGENT_SA_KEY_TPL.format(agent=agent_name)
    creds = json.dumps({"sa_b64": sa_b64})
    secrets_manager.set(
        vault_key,
        creds,
        description=f"Google SA for agent {agent_name}",
    )

    return RedirectResponse(url="/plugins/google-sa?saved=agent_sa", status_code=303)


@router.post("/agent-sa/delete")
async def delete_agent_sa(
    request: Request,
    agent_name: str = Form(...),
    csrf_token: str = Form(...),
    user: dict = Depends(require_login),
):
    """Remove agent SA from vault."""
    validate_csrf_token(request, csrf_token)

    known = {a["id"] for a in _get_agents_with_google_access()}
    if agent_name not in known:
        return RedirectResponse(
            url="/plugins/google-sa?error=invalid_agent", status_code=303
        )

    vault_key = AGENT_SA_KEY_TPL.format(agent=agent_name)
    secrets_manager.delete(vault_key)

    return RedirectResponse(url="/plugins/google-sa?deleted=agent_sa", status_code=303)
