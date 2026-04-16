"""Admin routes for WhatsApp Meta API plugin."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config.logging_config import logger
from ui.jinja_env import templates
from ui.plugin_helpers import get_plugin_template_context
from ui.routes.auth import require_login
from ui.secrets_manager import secrets_manager

router = APIRouter()

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _get_plugin_metadata() -> dict:
    """Plugin metadata for auto-sidebar."""
    return {
        "name": "whatsapp_api",
        "display_name": "WhatsApp (Meta API)",
        "icon": "fa-brands fa-whatsapp",
    }


@router.get("/", response_class=HTMLResponse)
@router.get("", response_class=HTMLResponse)
async def whatsapp_api_dashboard(request: Request, _=Depends(require_login)):
    """WhatsApp Meta API plugin dashboard."""
    base_url = os.getenv("GRIDBEAR_BASE_URL", "").rstrip("/")
    webhook_url = f"{base_url}/api/whatsapp_api/webhook"

    return templates.TemplateResponse(
        "whatsapp_api.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            webhook_url=webhook_url,
        ),
    )


@router.get("/authorized", response_class=JSONResponse)
async def list_authorized(request: Request, _=Depends(require_login)):
    """List all authorized numbers."""
    from plugins.whatsapp_api.models import AuthorizedNumber

    rows = AuthorizedNumber.search_sync([])
    return JSONResponse(
        [
            {
                "id": r["id"],
                "phone_number_id": r["phone_number_id"],
                "phone": r["phone"],
                "label": r.get("label", ""),
            }
            for r in rows
        ]
    )


@router.post("/authorized/add", response_class=JSONResponse)
async def add_authorized(request: Request, _=Depends(require_login)):
    """Add an authorized number."""
    from plugins.whatsapp_api.models import AuthorizedNumber

    body = await request.json()
    phone_number_id = body.get("phone_number_id", "").strip()
    phone = body.get("phone", "").strip()
    label = body.get("label", "").strip()

    if not phone_number_id or not phone:
        return JSONResponse(
            {"ok": False, "error": "phone_number_id and phone are required"},
            status_code=400,
        )

    try:
        AuthorizedNumber.create_sync(
            phone_number_id=phone_number_id,
            phone=phone,
            label=label or None,
        )
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/authorized/remove", response_class=JSONResponse)
async def remove_authorized(request: Request, _=Depends(require_login)):
    """Remove an authorized number."""
    from plugins.whatsapp_api.models import AuthorizedNumber

    body = await request.json()
    row_id = body.get("id")
    if not row_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)

    try:
        AuthorizedNumber.delete_sync(id=row_id)
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@router.post("/test")
async def send_test_message(request: Request, _=Depends(require_login)):
    """Send a test message via Meta WhatsApp API."""
    from plugins.whatsapp_api.meta_client import MetaClient

    try:
        body = await request.json()
        phone = body.get("phone", "")
        text = body.get("text", "")
        phone_number_id = body.get("phone_number_id", "")
        access_token_secret = body.get("access_token_secret", "")

        if not all([phone, text, phone_number_id, access_token_secret]):
            return JSONResponse(
                {"ok": False, "error": "All fields are required"},
                status_code=400,
            )

        token = secrets_manager.get_plain(access_token_secret)
        if not token:
            return JSONResponse(
                {"ok": False, "error": f"Secret '{access_token_secret}' not found"},
                status_code=400,
            )

        client = MetaClient(phone_number_id, token)
        await client.start()
        try:
            result = await client.send_text(phone, text)
            return JSONResponse({"ok": True, "result": result})
        finally:
            await client.close()

    except Exception as exc:
        logger.exception("WhatsApp API test message failed")
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500,
        )
