"""User portal routes for WhatsApp connections."""

from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import ChoiceLoader, FileSystemLoader

from config.logging_config import logger
from ui.jinja_env import templates
from ui.routes.auth import require_user

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ADMIN_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ui"

# Prepend plugin portal templates so plugin-specific templates are found first,
# then fall back to ui/templates/ for shared base templates (me/base.html etc.)
templates.env.loader = ChoiceLoader(
    [
        FileSystemLoader(str(PLUGIN_DIR / "portal" / "templates")),
        templates.env.loader,
    ]
)

router = APIRouter(prefix="/me/connections/whatsapp", tags=["whatsapp-portal"])


async def _evolution_api_request(method: str, path: str, **kwargs) -> dict | None:
    """Make a request to Evolution API."""
    from ui.secrets_manager import secrets_manager

    api_url = secrets_manager.get_plain(
        "EVOLUTION_API_URL", default="http://gridbear-evolution:8080"
    )
    api_key = secrets_manager.get_plain("EVOLUTION_API_KEY")
    if not api_key:
        return None

    url = f"{api_url.rstrip('/')}{path}"
    headers = {"apikey": api_key}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.request(method, url, **kwargs) as resp:
                if resp.status in (200, 201):
                    try:
                        return await resp.json()
                    except aiohttp.ContentTypeError:
                        return {"status": "ok"}
                text = await resp.text()
                return {"error": text, "status_code": resp.status}
    except Exception:
        logger.exception("Evolution API request failed: %s %s", method, path)
        return {"error": "Service unavailable"}


async def _verify_instance_ownership(instance_name: str, user: dict) -> bool:
    """Verify that a WhatsApp instance belongs to the requesting user."""
    try:
        from ..models import UserInstance

        inst = await UserInstance.get(instance_name=instance_name)
        if not inst:
            return False
        uid = user.get("unified_id") or user.get("username")
        return inst.unified_id == uid
    except Exception:
        return False


def _uid(user: dict) -> str:
    return user.get("unified_id") or user.get("username")


@router.get("/connect", response_class=HTMLResponse)
async def whatsapp_connect_page(
    request: Request,
    user: dict = Depends(require_user),
):
    """Show WhatsApp QR code connection page with user instances."""
    from core.models.agent_config import AgentConfigRecord

    available_agents = []

    try:
        records = AgentConfigRecord.search_sync([("is_active", "=", True)])
        for record in records:
            agent_data = dict(record)
            channels = agent_data.get("channels") or {}
            if "whatsapp" in channels:
                available_agents.append(
                    {
                        "id": agent_data.get("id", ""),
                        "name": agent_data.get("name", agent_data.get("id", "")),
                    }
                )
    except Exception:
        pass

    # Load user's personal instances from DB
    user_instances = []
    unified_id = user.get("unified_id", "")
    if unified_id:
        try:
            from ..models import UserInstance

            for agent_info in available_agents:
                inst = await UserInstance.get(
                    unified_id=unified_id, agent_name=agent_info["id"]
                )
                if inst:
                    status_data = await _evolution_api_request(
                        "GET", f"/instance/connectionState/{inst['instance_name']}"
                    )
                    state = "unknown"
                    if status_data and "error" not in status_data:
                        state = status_data.get("instance", {}).get("state", "unknown")
                    user_instances.append(
                        {
                            "instance_name": inst["instance_name"],
                            "agent_name": agent_info["name"],
                            "agent_id": agent_info["id"],
                            "state": state,
                            "connected": state == "open",
                        }
                    )
        except Exception:
            pass

    connected_agents = {i["agent_id"] for i in user_instances}
    connectable_agents = [
        a for a in available_agents if a["id"] not in connected_agents
    ]

    return templates.TemplateResponse(
        "me/connect_whatsapp.html",
        {
            "request": request,
            "user": user,
            "service": {"id": "whatsapp", "name": "WhatsApp"},
            "plugin_name": "whatsapp",
            "user_instances": user_instances,
            "connectable_agents": connectable_agents,
        },
    )


@router.post("/connect-instance")
async def whatsapp_connect_instance(
    request: Request,
    instance_name: str = Form(...),
    user: dict = Depends(require_user),
):
    """Connect a WhatsApp instance (create if needed, return QR)."""
    if not await _verify_instance_ownership(instance_name, user):
        return JSONResponse({"error": "Instance not found"}, status_code=404)
    result = await _evolution_api_request("GET", f"/instance/connect/{instance_name}")
    if result and "error" not in result:
        return JSONResponse(result)

    webhook_url = "http://gridbear:8000/api/whatsapp/webhook"
    create_result = await _evolution_api_request(
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
        connect_result = await _evolution_api_request(
            "GET", f"/instance/connect/{instance_name}"
        )
        if connect_result:
            return JSONResponse(connect_result)

    return JSONResponse(
        {"error": "Failed to create/connect instance"},
        status_code=500,
    )


@router.get("/status/{instance_name}")
async def whatsapp_instance_status(
    instance_name: str,
    user: dict = Depends(require_user),
):
    """Get WhatsApp instance connection status (for polling)."""
    if not await _verify_instance_ownership(instance_name, user):
        return JSONResponse({"state": "unknown", "connected": False})
    result = await _evolution_api_request(
        "GET", f"/instance/connectionState/{instance_name}"
    )
    if result and "error" not in result:
        state = result.get("instance", {}).get("state", "unknown")
        return JSONResponse({"state": state, "connected": state == "open"})
    return JSONResponse({"state": "unknown", "connected": False})


@router.post("/disconnect-instance")
async def whatsapp_disconnect_instance(
    request: Request,
    instance_name: str = Form(...),
    user: dict = Depends(require_user),
):
    """Disconnect a WhatsApp instance."""
    if not await _verify_instance_ownership(instance_name, user):
        return JSONResponse({"error": "Instance not found"}, status_code=404)
    result = await _evolution_api_request("DELETE", f"/instance/logout/{instance_name}")
    if result and "error" not in result:
        return JSONResponse({"status": "disconnected"})
    err_msg = str(result.get("error", "")) if result else ""
    if "not connected" in err_msg.lower():
        return JSONResponse({"status": "disconnected"})
    return JSONResponse(
        {"error": "Failed to disconnect"},
        status_code=500,
    )


@router.post("/create-instance")
async def whatsapp_create_instance(
    request: Request,
    agent_name: str = Form(...),
    user: dict = Depends(require_user),
):
    """Create a per-user WhatsApp instance for an agent."""
    unified_id = user.get("unified_id", "")
    if not unified_id:
        return JSONResponse({"error": "No unified ID"}, status_code=400)

    from ..models import UserInstance

    existing = await UserInstance.get(unified_id=unified_id, agent_name=agent_name)
    if existing:
        return JSONResponse(
            {
                "error": "Instance already exists",
                "instance_name": existing["instance_name"],
            },
            status_code=409,
        )

    safe_uid = unified_id.replace("@", "_").replace(".", "_")[:20]
    instance_name = f"{agent_name}_{safe_uid}"

    await UserInstance.create(
        unified_id=unified_id, agent_name=agent_name, instance_name=instance_name
    )

    from ui.secrets_manager import secrets_manager

    evo_api_key = secrets_manager.get_plain("EVOLUTION_API_KEY")
    webhook_url = "http://gridbear:8000/api/whatsapp/webhook"
    create_result = await _evolution_api_request(
        "POST",
        "/instance/create",
        json={
            "instanceName": instance_name,
            "integration": "WHATSAPP-BAILEYS",
            "webhook": {
                "url": webhook_url,
                "byEvents": False,
                "base64": False,
                "headers": {"apikey": evo_api_key},
                "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE"],
            },
        },
    )

    if not create_result or "error" in create_result:
        await UserInstance.delete_multi([("instance_name", "=", instance_name)])
        return JSONResponse(
            {"error": "Failed to create Evolution instance"},
            status_code=500,
        )

    # Register runtime client on the WhatsApp channel
    try:
        from core.registry import get_agent_manager

        agent_manager = get_agent_manager()
        if agent_manager:
            agent = agent_manager._agents.get(agent_name)
            if agent:
                wa_ch = agent.get_channel("whatsapp")
                if wa_ch:
                    await wa_ch.add_user_instance(unified_id, instance_name)
    except Exception:
        pass

    connect_result = await _evolution_api_request(
        "GET", f"/instance/connect/{instance_name}"
    )

    return JSONResponse(
        {
            "instance_name": instance_name,
            "qr": connect_result
            if connect_result and "error" not in connect_result
            else None,
        }
    )


@router.post("/{instance_name}/delete")
async def whatsapp_delete_user_instance(
    request: Request,
    instance_name: str,
    user: dict = Depends(require_user),
):
    """Delete a per-user WhatsApp instance."""
    unified_id = user.get("unified_id", "")
    from ..models import UserInstance

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    try:
        from core.registry import get_agent_manager

        agent_manager = get_agent_manager()
        if agent_manager:
            agent = agent_manager._agents.get(inst["agent_name"])
            if agent:
                wa_ch = agent.get_channel("whatsapp")
                if wa_ch:
                    await wa_ch.remove_user_instance(instance_name)
    except Exception:
        pass

    await _evolution_api_request("DELETE", f"/instance/delete/{instance_name}")
    await UserInstance.delete(inst["id"])

    return JSONResponse({"status": "deleted"})


@router.get("/{instance_name}/authorized", response_class=HTMLResponse)
async def whatsapp_authorized_numbers(
    request: Request,
    instance_name: str,
    user: dict = Depends(require_user),
):
    """Show authorized numbers management page for a user instance."""
    unified_id = user.get("unified_id", "")
    from ..models import AuthorizedNumber, UserInstance, WakeWord

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return RedirectResponse(url="/me/connections", status_code=303)

    raw_numbers = await AuthorizedNumber.search(
        [("instance_id", "=", inst["id"])], order="created_at"
    )
    numbers = [{"phone": n["phone"], "label": n["label"]} for n in raw_numbers]

    wake_words = await WakeWord.search(
        [("instance_id", "=", inst["id"])], order="keyword"
    )

    status_data = await _evolution_api_request(
        "GET", f"/instance/connectionState/{instance_name}"
    )
    state = "unknown"
    if status_data and "error" not in status_data:
        state = status_data.get("instance", {}).get("state", "unknown")

    return templates.TemplateResponse(
        "me/whatsapp_authorized.html",
        {
            "request": request,
            "user": user,
            "instance": inst,
            "instance_state": state,
            "numbers": numbers,
            "wake_words": wake_words,
        },
    )


@router.post("/{instance_name}/authorized/add")
async def whatsapp_add_authorized(
    request: Request,
    instance_name: str,
    phone: str = Form(...),
    label: str = Form(""),
    user: dict = Depends(require_user),
):
    """Add an authorized phone number to a user instance."""
    unified_id = user.get("unified_id", "")
    from ..models import AuthorizedNumber, UserInstance

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    clean_phone = "".join(c for c in phone if c.isdigit())
    if not clean_phone or len(clean_phone) < 8:
        return JSONResponse({"error": "Invalid phone number"}, status_code=400)

    result = await AuthorizedNumber.add_number(inst["id"], clean_phone, label.strip())
    if result is None:
        return JSONResponse({"error": "Number already authorized"}, status_code=409)

    return JSONResponse(
        {
            "status": "added",
            "number": {"phone": result["phone"], "label": result["label"]},
        }
    )


@router.post("/{instance_name}/authorized/remove")
async def whatsapp_remove_authorized(
    request: Request,
    instance_name: str,
    phone: str = Form(...),
    user: dict = Depends(require_user),
):
    """Remove an authorized phone number from a user instance."""
    unified_id = user.get("unified_id", "")
    from ..models import AuthorizedNumber, UserInstance

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    removed = await AuthorizedNumber.delete_multi(
        [("instance_id", "=", inst["id"]), ("phone", "=", phone)]
    )
    if not removed:
        return JSONResponse({"error": "Number not found"}, status_code=404)

    return JSONResponse({"status": "removed"})


@router.post("/{instance_name}/authorized/settings")
async def whatsapp_reject_settings(
    request: Request,
    instance_name: str,
    silent_reject: str = Form("off"),
    reject_message: str = Form(""),
    user: dict = Depends(require_user),
):
    """Update reject behavior for unauthorized numbers."""
    unified_id = user.get("unified_id", "")
    from ..models import UserInstance

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    is_silent = silent_reject == "on"
    await UserInstance.write(
        inst["id"], silent_reject=is_silent, reject_message=reject_message.strip()
    )

    return JSONResponse({"status": "updated"})


@router.post("/{instance_name}/authorized/wake-word/add")
async def whatsapp_add_wake_word(
    request: Request,
    instance_name: str,
    keyword: str = Form(...),
    response: str = Form(...),
    user: dict = Depends(require_user),
):
    """Add a wake word auto-response for unauthorized senders."""
    unified_id = user.get("unified_id", "")
    from ..models import UserInstance, WakeWord

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    kw = keyword.lower().strip()
    resp_text = response.strip()
    if not kw or not resp_text:
        return JSONResponse(
            {"error": "Keyword and response are required"}, status_code=400
        )

    result = await WakeWord.add_word(inst["id"], kw, resp_text)
    if result is None:
        return JSONResponse({"error": "Wake word already exists"}, status_code=409)

    return JSONResponse({"status": "added", "wake_word": result})


@router.post("/{instance_name}/authorized/wake-word/remove")
async def whatsapp_remove_wake_word(
    request: Request,
    instance_name: str,
    keyword: str = Form(...),
    user: dict = Depends(require_user),
):
    """Remove a wake word auto-response."""
    unified_id = user.get("unified_id", "")
    from ..models import UserInstance, WakeWord

    inst = await UserInstance.get(instance_name=instance_name)
    if not inst or inst["unified_id"] != unified_id:
        return JSONResponse({"error": "Not found or not authorized"}, status_code=403)

    removed = await WakeWord.delete_multi(
        [("instance_id", "=", inst["id"]), ("keyword", "=", keyword.lower().strip())]
    )
    if not removed:
        return JSONResponse({"error": "Wake word not found"}, status_code=404)

    return JSONResponse({"status": "removed"})
