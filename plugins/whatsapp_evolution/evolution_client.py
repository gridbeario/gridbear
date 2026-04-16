"""Evolution API v2 HTTP client for WhatsApp integration."""

import asyncio
import base64
from pathlib import Path

import aiohttp

from config.logging_config import logger


class EvolutionError(Exception):
    """Base error for Evolution API."""


class EvolutionAuthError(EvolutionError):
    """Authentication failed (401)."""


class EvolutionNotFoundError(EvolutionError):
    """Resource not found (404)."""


class EvolutionServerError(EvolutionError):
    """Server error (5xx)."""


class EvolutionConnectionError(EvolutionError):
    """Cannot connect to Evolution API."""


class EvolutionClientError(EvolutionError):
    """Client error (4xx, excluding 401/404)."""


class EvolutionClient:
    """HTTP client for Evolution API v2."""

    RETRY_DELAYS = [1, 2, 4]

    def __init__(self, base_url: str, api_key: str, instance_name: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance_name = instance_name
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        """Create HTTP session."""
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"apikey": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            )

    async def stop(self):
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        retryable: bool = True,
        **kwargs,
    ) -> dict | list | None:
        """Make HTTP request with retry and error handling."""
        if not self._session or self._session.closed:
            await self.start()

        url = f"{self.base_url}{path}"
        last_error = None

        delays = self.RETRY_DELAYS if retryable else [0]
        for attempt, delay in enumerate(delays):
            if attempt > 0:
                logger.debug(
                    f"Evolution API retry {attempt} after {delay}s: {method} {path}"
                )
                await asyncio.sleep(delay)

            try:
                async with self._session.request(method, url, **kwargs) as resp:
                    if resp.status == 200 or resp.status == 201:
                        try:
                            return await resp.json()
                        except aiohttp.ContentTypeError:
                            return None

                    body = await resp.text()

                    if resp.status == 401:
                        raise EvolutionAuthError(f"Authentication failed: {body}")
                    elif resp.status == 404:
                        raise EvolutionNotFoundError(f"Not found: {path} - {body}")
                    elif resp.status == 429:
                        last_error = EvolutionServerError(f"Rate limited: {body}")
                        continue
                    elif resp.status >= 500:
                        last_error = EvolutionServerError(
                            f"Server error {resp.status}: {body}"
                        )
                        continue
                    else:
                        raise EvolutionClientError(
                            f"Client error {resp.status}: {body}"
                        )

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = EvolutionConnectionError(f"Connection error: {e}")
                if not retryable:
                    raise last_error

        if last_error:
            raise last_error
        return None

    # --- Instance Management ---

    async def create_instance(self, webhook_url: str) -> dict:
        """Create a new Evolution API instance."""
        return await self._request(
            "POST",
            "/instance/create",
            json={
                "instanceName": self.instance_name,
                "integration": "WHATSAPP-BAILEYS",
                "webhook": {
                    "url": webhook_url,
                    "byEvents": False,
                    "base64": False,
                    "events": [
                        "MESSAGES_UPSERT",
                        "CONNECTION_UPDATE",
                    ],
                },
            },
            retryable=False,
        )

    async def connect_instance(self) -> dict:
        """Connect instance and get QR code."""
        return await self._request(
            "GET",
            f"/instance/connect/{self.instance_name}",
            retryable=False,
        )

    async def get_connection_status(self) -> dict:
        """Get instance connection state."""
        return await self._request(
            "GET",
            f"/instance/connectionState/{self.instance_name}",
        )

    async def logout_instance(self) -> dict:
        """Logout and disconnect instance."""
        return await self._request(
            "DELETE",
            f"/instance/logout/{self.instance_name}",
            retryable=False,
        )

    async def set_webhook(self, webhook_url: str) -> dict:
        """Set webhook URL for instance."""
        return await self._request(
            "POST",
            f"/webhook/set/{self.instance_name}",
            json={
                "url": webhook_url,
                "byEvents": False,
                "base64": False,
                "events": [
                    "MESSAGES_UPSERT",
                    "CONNECTION_UPDATE",
                ],
            },
        )

    # --- Messaging ---

    async def send_text(self, phone: str, text: str) -> dict:
        """Send a text message."""
        return await self._request(
            "POST",
            f"/message/sendText/{self.instance_name}",
            json={
                "number": phone,
                "text": text,
            },
        )

    async def send_media(
        self,
        phone: str,
        media_type: str,
        file_path_or_url: str,
        caption: str | None = None,
    ) -> dict:
        """Send media message (image, video, document)."""
        payload = {
            "number": phone,
            "mediatype": media_type,
            "caption": caption or "",
        }

        path = Path(file_path_or_url)
        if path.exists():
            with open(path, "rb") as f:
                media_b64 = base64.b64encode(f.read()).decode()
            payload["media"] = media_b64
            payload["fileName"] = path.name
        else:
            payload["media"] = file_path_or_url

        return await self._request(
            "POST",
            f"/message/sendMedia/{self.instance_name}",
            json=payload,
        )

    async def send_audio(self, phone: str, file_path: str) -> dict:
        """Send audio as WhatsApp voice note (PTT)."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {file_path}")

        with open(path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()

        return await self._request(
            "POST",
            f"/message/sendWhatsAppAudio/{self.instance_name}",
            json={
                "number": phone,
                "audio": audio_b64,
            },
        )

    # --- Presence & Media Download ---

    async def send_presence(self, state: str, phone: str) -> dict | None:
        """Update chat presence (composing, recording, paused)."""
        try:
            return await self._request(
                "POST",
                f"/chat/presence/{self.instance_name}",
                json={
                    "number": phone,
                    "presence": state,
                },
                retryable=False,
            )
        except Exception as e:
            logger.debug(f"Presence update failed (non-critical): {e}")
            return None

    async def download_media(self, message_data: dict) -> str | None:
        """Download media from a message, returns base64 string."""
        try:
            result = await self._request(
                "POST",
                f"/chat/getBase64FromMediaMessage/{self.instance_name}",
                json={"message": message_data},
            )
            if result and isinstance(result, dict):
                return result.get("base64")
            return None
        except Exception as e:
            logger.error(f"Failed to download media: {e}")
            return None
