"""User portal routes for the artifacts plugin.

Registered by ui.plugin_admin.PluginAdminRegistry.register_portal_routes on
startup (ui/app.py).  Also registers the `hmac_token` Jinja filter so the
template can build signed capability URLs without leaking signing internals
into the core UI.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from plugins.artifacts.models import Artifact
from plugins.artifacts.signing import sign_uuid
from ui.jinja_env import templates
from ui.routes.auth import require_user

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["user-portal"])


# Register the `hmac_token` Jinja filter at module import time.
# This module is imported during UI startup via register_portal_routes,
# which keeps the plugin→core→plugin dependency direction clean (core
# does not import from plugins; the plugin wires itself into core).
def _hmac_token_filter(uuid_str: str) -> str:
    try:
        return sign_uuid(uuid_str)
    except Exception:
        return ""


templates.env.filters["hmac_token"] = _hmac_token_filter


@router.get("/artifacts", response_class=HTMLResponse)
async def me_artifacts(request: Request, user: dict = Depends(require_user)):
    """Show the signed-in user's own artifacts with capability links."""
    username = user["username"]
    try:
        rows = await Artifact.search(
            [("owner_user_id", "=", username)],
            order="created_at DESC",
            limit=100,
        )
    except Exception as exc:
        _logger.warning("Failed to load artifacts for %s: %s", username, exc)
        rows = []

    return templates.TemplateResponse(
        request,
        "me_artifacts.html",
        {"request": request, "user": user, "artifacts": rows},
    )
