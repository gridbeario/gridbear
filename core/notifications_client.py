"""Lightweight HTTP client to send notifications to the UI container."""

import os

import httpx

from config.logging_config import logger

# The UI container URL -- same env var used by MCP Gateway config
_UI_URL = os.getenv("MCP_GATEWAY_URL", "http://localhost:8000")
_SECRET = os.getenv("INTERNAL_API_SECRET", "")


async def send_notification(
    category: str,
    severity: str,
    title: str,
    message: str = "",
    source: str = "",
    user_id: str | None = None,
    action_url: str | None = None,
):
    """Fire-and-forget notification to the UI container via internal API.

    Silently fails -- notifications are best-effort, never block the bot.
    """
    if not _SECRET:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{_UI_URL}/notifications/internal/create",
                json={
                    "category": category,
                    "severity": severity,
                    "title": title,
                    "message": message,
                    "source": source,
                    "user_id": user_id,
                    "action_url": action_url,
                },
                headers={"Authorization": f"Bearer {_SECRET}"},
            )
    except Exception as exc:
        logger.debug("Failed to send notification to UI: %s", exc)
