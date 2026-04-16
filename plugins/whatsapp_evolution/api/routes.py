"""WhatsApp Internal API endpoints.

Receives webhook events from Evolution API.
Previously hardcoded in core/internal_api/server.py.
"""

import asyncio
import hmac

from fastapi import APIRouter, HTTPException, Request

from config.logging_config import logger
from core.registry import get_agent_manager

router = APIRouter()


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    """Receive webhook events from Evolution API.

    NOT using verify_internal_auth -- validates Evolution API's apikey header instead.
    """
    from ui.secrets_manager import secrets_manager

    # Validate Evolution API key
    apikey = request.headers.get("apikey", "")
    expected_key = secrets_manager.get_plain("EVOLUTION_API_KEY")
    if not expected_key or not hmac.compare_digest(apikey, expected_key):
        raise HTTPException(status_code=403, detail="Invalid API key")

    payload = await request.json()

    # Find the matching WhatsApp channel by instance name
    instance_name = payload.get("instance", "")
    logger.debug(
        f"WhatsApp webhook: instance='{instance_name}', keys={list(payload.keys())}"
    )
    agent_manager = get_agent_manager()
    if not agent_manager:
        raise HTTPException(status_code=503, detail="Agent manager not available")

    channel = None
    for agent in agent_manager._agents.values():
        wa_channel = agent.get_channel("whatsapp")
        if wa_channel and getattr(wa_channel, "instance_name", "") == instance_name:
            channel = wa_channel
            break

    # Check user instances on channel objects
    if not channel:
        for agent in agent_manager._agents.values():
            wa_ch = agent.get_channel("whatsapp")
            if wa_ch and instance_name in getattr(wa_ch, "_user_clients", {}):
                channel = wa_ch
                break

    if not channel:
        # Try matching any WhatsApp channel if only one exists
        wa_channels = []
        for agent in agent_manager._agents.values():
            wa_ch = agent.get_channel("whatsapp")
            if wa_ch:
                wa_channels.append(wa_ch)
        if len(wa_channels) == 1:
            channel = wa_channels[0]

    if not channel:
        logger.warning(
            f"WhatsApp webhook: no channel found for instance '{instance_name}'"
        )
        return {"status": "ok"}  # Return 200 to avoid Evolution API retries

    # Process in background, passing instance_name for multi-tenant routing
    asyncio.create_task(channel._handle_webhook(payload, instance_name=instance_name))
    return {"status": "ok"}
