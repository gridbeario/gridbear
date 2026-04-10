"""Admin routes for WhatsApp plugin."""

from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ui.jinja_env import templates
from ui.plugin_helpers import get_plugin_template_context
from ui.routes.auth import require_login
from ui.secrets_manager import secrets_manager

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _get_evolution_config() -> tuple[str, str]:
    """Get Evolution API URL and key from secrets/env."""
    api_url = secrets_manager.get_plain(
        "EVOLUTION_API_URL", default="http://gridbear-evolution:8080"
    )
    api_key = secrets_manager.get_plain("EVOLUTION_API_KEY")
    return api_url, api_key


async def _evolution_request(method: str, path: str, **kwargs) -> dict | None:
    """Make a request to Evolution API."""
    api_url, api_key = _get_evolution_config()
    if not api_key:
        return None

    url = f"{api_url.rstrip('/')}{path}"
    headers = {"apikey": api_key}

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.request(method, url, **kwargs) as resp:
            if resp.status in (200, 201):
                try:
                    return await resp.json()
                except aiohttp.ContentTypeError:
                    return {"status": "ok"}
            text = await resp.text()
            return {"error": text, "status_code": resp.status}


async def _get_instances_status() -> list[dict]:
    """Get status of all WhatsApp instances (DB + user instances)."""
    instances = []

    # DB-configured instances
    try:
        from core.models.agent_config import AgentConfigRecord

        records = AgentConfigRecord.search_sync([("is_active", "=", True)])
        for record in records:
            agent_data = dict(record)
            channels = agent_data.get("channels") or {}
            if "whatsapp" in channels:
                wa_config = channels["whatsapp"]
                instance_name = wa_config.get("instance_name", agent_data.get("id", ""))
                agent_name = agent_data.get("name", agent_data.get("id", ""))

                status_data = await _evolution_request(
                    "GET", f"/instance/connectionState/{instance_name}"
                )
                state = "unknown"
                if status_data and "error" not in status_data:
                    state = status_data.get("instance", {}).get("state", "unknown")

                instances.append(
                    {
                        "instance_name": instance_name,
                        "agent_name": agent_name,
                        "state": state,
                        "connected": state == "open",
                        "user_instance": False,
                    }
                )
    except Exception:
        pass

    # User-created instances from DB
    try:
        from core.registry import get_agent_manager
        from plugins.whatsapp.models import UserInstance

        agent_manager = get_agent_manager()
        if agent_manager:
            for agent in agent_manager._agents.values():
                wa_ch = agent.get_channel("whatsapp")
                if not wa_ch:
                    continue
                agent_name = getattr(wa_ch, "agent_name", "")
                if not agent_name:
                    continue
                for inst in await UserInstance.search(
                    [("agent_name", "=", agent_name)], order="created_at"
                ):
                    status_data = await _evolution_request(
                        "GET", f"/instance/connectionState/{inst['instance_name']}"
                    )
                    state = "unknown"
                    if status_data and "error" not in status_data:
                        state = status_data.get("instance", {}).get("state", "unknown")

                    instances.append(
                        {
                            "instance_name": inst["instance_name"],
                            "agent_name": agent_name,
                            "state": state,
                            "connected": state == "open",
                            "user_instance": True,
                            "owner_id": inst["unified_id"],
                        }
                    )
    except Exception:
        pass

    return instances


@router.get("/", response_class=HTMLResponse)
async def whatsapp_dashboard(request: Request, _=Depends(require_login)):
    """WhatsApp dashboard with connection status."""
    _, api_key = _get_evolution_config()
    instances = await _get_instances_status() if api_key else []

    return templates.TemplateResponse(
        "whatsapp/index.html",
        get_plugin_template_context(
            request,
            PLUGIN_DIR,
            api_key_configured=bool(api_key),
            instances=instances,
        ),
    )


@router.post("/connect")
async def connect_instance(
    request: Request,
    instance_name: str = Form(...),
    _=Depends(require_login),
):
    """Create instance and get QR code."""
    api_url, api_key = _get_evolution_config()
    if not api_key:
        raise HTTPException(400, "Evolution API key not configured")

    # First try to connect existing instance
    result = await _evolution_request("GET", f"/instance/connect/{instance_name}")
    if result and "error" not in result:
        return JSONResponse(result)

    # If instance doesn't exist, create it
    webhook_url = "http://gridbear:8000/api/whatsapp/webhook"
    create_result = await _evolution_request(
        "POST",
        "/instance/create",
        json={
            "instanceName": instance_name,
            "integration": "WHATSAPP-BAILEYS",
            "webhook": {
                "url": webhook_url,
                "byEvents": False,
                "base64": False,
                "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],
            },
        },
    )

    if create_result and "error" not in create_result:
        # Now connect to get QR
        connect_result = await _evolution_request(
            "GET", f"/instance/connect/{instance_name}"
        )
        if connect_result:
            return JSONResponse(connect_result)

    return JSONResponse(
        {"error": "Failed to create/connect instance", "details": create_result},
        status_code=500,
    )


@router.get("/qr/{instance_name}")
async def get_qr_code(instance_name: str, _=Depends(require_login)):
    """Get QR code for instance (for polling)."""
    result = await _evolution_request("GET", f"/instance/connect/{instance_name}")
    if result:
        return JSONResponse(result)
    return JSONResponse({"error": "Failed to get QR code"}, status_code=500)


@router.get("/status/{instance_name}")
async def get_instance_status(instance_name: str, _=Depends(require_login)):
    """Get connection status for instance (for AJAX polling)."""
    result = await _evolution_request(
        "GET", f"/instance/connectionState/{instance_name}"
    )
    if result and "error" not in result:
        state = result.get("instance", {}).get("state", "unknown")
        return JSONResponse({"state": state, "connected": state == "open"})
    return JSONResponse({"state": "unknown", "connected": False})


@router.post("/disconnect")
async def disconnect_instance(
    request: Request,
    instance_name: str = Form(...),
    _=Depends(require_login),
):
    """Disconnect/logout instance."""
    result = await _evolution_request("DELETE", f"/instance/logout/{instance_name}")
    if result and "error" not in result:
        return JSONResponse({"status": "disconnected"})
    return JSONResponse(
        {"error": "Failed to disconnect", "details": result},
        status_code=500,
    )


@router.post("/test")
async def send_test_message(
    request: Request,
    instance_name: str = Form(...),
    phone: str = Form(...),
    _=Depends(require_login),
):
    """Send a test message."""
    result = await _evolution_request(
        "POST",
        f"/message/sendText/{instance_name}",
        json={
            "number": phone,
            "text": "🐻 GridBear WhatsApp test message!",
        },
    )
    if result and "error" not in result:
        return JSONResponse({"status": "sent"})
    return JSONResponse(
        {"error": "Failed to send test message", "details": result},
        status_code=500,
    )


@router.get("/health")
async def health_check(_=Depends(require_login)):
    """Health check for all instances."""
    instances = await _get_instances_status()
    all_ok = all(i["connected"] for i in instances) if instances else False
    return JSONResponse(
        {
            "status": "ok" if all_ok else "degraded",
            "instances": instances,
        }
    )
