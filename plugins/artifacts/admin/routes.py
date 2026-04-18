"""Admin routes for the artifacts plugin."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ui.jinja_env import templates
from ui.plugin_helpers import get_plugin_template_context
from ui.routes.auth import require_login

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _get_plugin_metadata() -> dict:
    return {
        "name": "artifacts",
        "display_name": "Artifacts",
        "icon": "fa-solid fa-cubes",
    }


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def list_artifacts(
    request: Request,
    _=Depends(require_login),
    agent: str | None = None,
    owner: str | None = None,
    status: str | None = None,
):
    from plugins.artifacts.models import Artifact

    domain: list = []
    if agent:
        domain.append(("agent_id", "=", agent))
    if owner:
        domain.append(("owner_user_id", "=", owner))
    rows = await Artifact.search(domain, order="created_at DESC", limit=200)

    now = datetime.now(UTC)
    enriched = []
    for r in rows:
        if r["revoked_at"]:
            s = "revoked"
        elif r["expires_at"] < now and not r["pinned"]:
            s = "expired"
        else:
            s = "active"
        if status and status != s:
            continue
        r = dict(r)
        r["_status"] = s
        enriched.append(r)

    return templates.TemplateResponse(
        "list.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            artifacts=enriched,
            filter_agent=agent or "",
            filter_owner=owner or "",
            filter_status=status or "",
        ),
    )


@router.get("/{uuid}", response_class=HTMLResponse)
async def artifact_detail(request: Request, uuid: str, _=Depends(require_login)):
    from plugins.artifacts.models import Artifact
    from plugins.artifacts.signing import build_capability_url

    rows = await Artifact.search([("id", "=", uuid)])
    if not rows:
        return HTMLResponse("Not Found", status_code=404)
    row = dict(rows[0])
    row["capability_url"] = build_capability_url(uuid)
    return templates.TemplateResponse(
        "detail.html",
        get_plugin_template_context(request, PLUGIN_DIR, artifact=row),
    )


@router.post("/{uuid}/pin")
async def admin_pin(uuid: str, pinned: str = Form(...), _=Depends(require_login)):
    from plugins.artifacts.service import ArtifactsService

    # HTML checkbox sends "true"/"false" as string
    await ArtifactsService().pin(uuid, pinned=pinned.lower() == "true")
    return RedirectResponse(f"/plugin/artifacts/{uuid}", status_code=303)


@router.post("/{uuid}/revoke")
async def admin_revoke(uuid: str, _=Depends(require_login)):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().revoke(uuid)
    return RedirectResponse(f"/plugin/artifacts/{uuid}", status_code=303)


@router.post("/{uuid}/unrevoke")
async def admin_unrevoke(uuid: str, _=Depends(require_login)):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().unrevoke(uuid)
    return RedirectResponse(f"/plugin/artifacts/{uuid}", status_code=303)


@router.post("/{uuid}/delete")
async def admin_delete(uuid: str, _=Depends(require_login)):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().hard_delete(uuid)
    return RedirectResponse("/plugin/artifacts/", status_code=303)
