"""Public routes for the artifacts plugin.

Mounted on the UI FastAPI app at prefix /artifacts. These endpoints do not
require admin login — access is gated by the HMAC token (capability model).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from plugins.artifacts import storage
from plugins.artifacts.models import Artifact
from plugins.artifacts.signing import verify_signature

_logger = logging.getLogger(__name__)

router = APIRouter()

_CDNS = ["https://esm.sh", "https://unpkg.com", "https://cdn.jsdelivr.net"]


def _embed_csp() -> str:
    cdns = " ".join(_CDNS)
    return (
        "default-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'unsafe-inline' {cdns}; "
        f"style-src 'self' 'unsafe-inline' {cdns}; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https:; "
        "connect-src 'none'; "
        "frame-ancestors 'self'; "
        "form-action 'none'; "
        "base-uri 'self'"
    )


def _wrapper_csp() -> str:
    return (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-src 'self'; "
        "frame-ancestors 'self'"
    )


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _wrapper_html(*, title: str, uid: str, token: str, size_bytes: int) -> str:
    size_kb = size_bytes // 1024 or 1
    safe_title = _html_escape(title)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <meta property="og:title" content="{safe_title}">
  <meta property="og:description" content="GridBear artifact — {size_kb}KB">
  <meta property="og:type" content="website">
  <style>
    body {{ margin: 0; font-family: system-ui, sans-serif; }}
    .top-bar {{ display: flex; justify-content: space-between; align-items: center;
               padding: 12px 16px; background: #1a1a1a; color: #fff; height: 48px; }}
    iframe {{ width: 100%; height: calc(100vh - 48px); border: 0; display: block; }}
  </style>
</head>
<body>
  <header class="top-bar"><h1>{safe_title}</h1></header>
  <iframe src="/artifacts/{uid}?t={token}&amp;mode=embed"
          sandbox="allow-scripts" allow="clipboard-write"></iframe>
</body>
</html>"""


@router.get("/{uuid}")
async def serve_artifact(
    request: Request,
    uuid: str,
    t: str = Query(..., min_length=32, max_length=64),
    mode: str | None = Query(None),
):
    if not verify_signature(uuid, t):
        return PlainTextResponse("Forbidden", status_code=403)

    rows = await Artifact.search([("id", "=", uuid)])
    if not rows:
        return PlainTextResponse("Not Found", status_code=404)

    row = rows[0]
    if row.get("revoked_at") is not None:
        return PlainTextResponse("This artifact has been revoked.", status_code=410)

    expires_at = row["expires_at"]
    if expires_at and expires_at < datetime.now(UTC) and not row["pinned"]:
        return PlainTextResponse("This artifact has expired.", status_code=410)

    if mode == "embed":
        if not storage.exists(uuid):
            _logger.warning("Artifact row %s exists but file missing", uuid)
            return PlainTextResponse("Not Found", status_code=404)
        html = storage.read_artifact(uuid)
        return HTMLResponse(
            content=html,
            headers={
                "Content-Security-Policy": _embed_csp(),
                "X-Frame-Options": "SAMEORIGIN",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    return HTMLResponse(
        content=_wrapper_html(
            title=row["title"],
            uid=uuid,
            token=t,
            size_bytes=row["size_bytes"],
        ),
        headers={
            "Content-Security-Policy": _wrapper_csp(),
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get("/{uuid}/meta")
async def artifact_meta(uuid: str):
    rows = await Artifact.search([("id", "=", uuid)])
    if not rows:
        return JSONResponse({"error": "not_found"}, status_code=404)
    row = rows[0]
    return JSONResponse(
        {
            "title": row["title"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "agent_id": row["agent_id"],
        }
    )


class PinBody(BaseModel):
    pinned: bool


@router.post("/{uuid}/pin")
async def action_pin(uuid: str, body: PinBody):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().pin(uuid, pinned=body.pinned)
    return {"ok": True}


@router.post("/{uuid}/revoke")
async def action_revoke(uuid: str):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().revoke(uuid)
    return {"ok": True}


@router.post("/{uuid}/unrevoke")
async def action_unrevoke(uuid: str):
    from plugins.artifacts.service import ArtifactsService

    await ArtifactsService().unrevoke(uuid)
    return {"ok": True}
