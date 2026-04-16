from __future__ import annotations

import asyncio
import hashlib
import hmac

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from config.logging_config import logger

router = APIRouter()


def _get_app_secret() -> str:
    from ui.secrets_manager import secrets_manager

    return secrets_manager.get_plain("WHATSAPP_API_APP_SECRET") or ""


def _get_verify_token() -> str:
    from ui.secrets_manager import secrets_manager

    return secrets_manager.get_plain("WHATSAPP_API_VERIFY_TOKEN") or ""


def _verify_signature(body: bytes, signature: str, app_secret: str) -> bool:
    """Validate X-Hub-Signature-256 header using HMAC-SHA256.

    Meta sends the signature as ``sha256={hex_digest}``.
    """
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


@router.get("/webhook")
async def webhook_verify(request: Request):
    """Meta verification handshake."""
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and hmac.compare_digest(token, _get_verify_token()):
        logger.info("WhatsApp Meta webhook verified")
        return PlainTextResponse(challenge)

    logger.warning("WhatsApp Meta webhook verification failed")
    return PlainTextResponse("Forbidden", status_code=403)


@router.post("/webhook")
async def webhook_receive(request: Request):
    """Receive incoming events from Meta Cloud API."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not _verify_signature(body, signature, _get_app_secret()):
        logger.warning("WhatsApp Meta webhook: invalid HMAC signature")
        return PlainTextResponse("Forbidden", status_code=403)

    payload = await request.json()
    asyncio.create_task(_dispatch_events(payload))
    return {"status": "ok"}


async def _dispatch_events(payload: dict) -> None:
    """Route incoming Meta webhook events to the appropriate channel."""
    from plugins.whatsapp_api import registry

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue

            channel = registry.get_channel(phone_number_id)
            if not channel:
                logger.warning(
                    "WhatsApp Meta webhook: no channel for phone_number_id=%s "
                    "(registry keys: %s)",
                    phone_number_id,
                    registry.get_all_keys(),
                )
                continue

            for msg in value.get("messages", []):
                try:
                    await channel.handle_incoming(msg)
                except Exception:
                    logger.exception(
                        "WhatsApp Meta webhook: error handling message "
                        "from phone_number_id=%s",
                        phone_number_id,
                    )
