from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp

from config.logging_config import logger

BASE_URL = "https://graph.facebook.com/v25.0"


class MetaApiError(Exception):
    """Base exception for Meta API errors."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Meta API error {status}: {message}")


class MetaAuthError(MetaApiError):
    """Raised on 401 authentication errors."""


class MetaRateLimitError(MetaApiError):
    """Raised on 429 rate limit errors after retries are exhausted."""


class MetaClient:
    """HTTP client for the Meta WhatsApp Cloud API."""

    def __init__(self, phone_number_id: str, access_token: str):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        """Create the aiohttp session with auth header and timeout."""
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.access_token}",
            },
            timeout=timeout,
        )

    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, url: str, retries: int = 3, **kwargs) -> dict:
        """Generic request with retry on 429/5xx, backoff 1,2,4s."""
        if not self._session:
            raise RuntimeError("MetaClient session not started, call start() first")

        last_exception: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._session.request(method, url, **kwargs) as resp:
                    if resp.status == 401:
                        body = await resp.text()
                        raise MetaAuthError(401, body)

                    if resp.status == 429 or resp.status >= 500:
                        body = await resp.text()
                        if attempt < retries - 1:
                            backoff = 2**attempt  # 1, 2, 4
                            logger.warning(
                                "Meta API %s %s returned %s, retrying in %ss "
                                "(attempt %d/%d)",
                                method,
                                url,
                                resp.status,
                                backoff,
                                attempt + 1,
                                retries,
                            )
                            await asyncio.sleep(backoff)
                            continue
                        if resp.status == 429:
                            raise MetaRateLimitError(429, body)
                        raise MetaApiError(resp.status, body)

                    if resp.status >= 400:
                        body = await resp.text()
                        raise MetaApiError(resp.status, body)

                    return await resp.json()

            except (MetaAuthError, MetaApiError):
                raise
            except aiohttp.ClientError as exc:
                last_exception = exc
                if attempt < retries - 1:
                    backoff = 2**attempt
                    logger.warning(
                        "Meta API %s %s client error: %s, retrying in %ss",
                        method,
                        url,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise MetaApiError(0, str(exc)) from exc

        raise MetaApiError(0, str(last_exception))  # pragma: no cover

    @property
    def _messages_url(self) -> str:
        return f"{BASE_URL}/{self.phone_number_id}/messages"

    async def send_text(self, phone: str, text: str) -> dict:
        """Send a text message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        }
        return await self._request("POST", self._messages_url, json=payload)

    async def send_image(
        self, phone: str, media_id: str, caption: str | None = None
    ) -> dict:
        """Send an image message."""
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = caption
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "image",
            "image": image,
        }
        return await self._request("POST", self._messages_url, json=payload)

    async def send_document(
        self,
        phone: str,
        media_id: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> dict:
        """Send a document message."""
        document: dict = {"id": media_id}
        if caption:
            document["caption"] = caption
        if filename:
            document["filename"] = filename
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "document",
            "document": document,
        }
        return await self._request("POST", self._messages_url, json=payload)

    async def send_audio(self, phone: str, media_id: str) -> dict:
        """Send an audio message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "audio",
            "audio": {"id": media_id},
        }
        return await self._request("POST", self._messages_url, json=payload)

    async def send_reaction(self, phone: str, message_id: str, emoji: str) -> dict:
        """Send a reaction to a message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "reaction",
            "reaction": {"message_id": message_id, "emoji": emoji},
        }
        return await self._request("POST", self._messages_url, json=payload)

    async def mark_read(self, message_id: str) -> None:
        """Mark a message as read."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        await self._request("POST", self._messages_url, json=payload)

    async def upload_media(self, file_path: str, mime_type: str) -> str:
        """Upload media and return the media_id."""
        url = f"{BASE_URL}/{self.phone_number_id}/media"
        data = aiohttp.FormData()
        data.add_field("messaging_product", "whatsapp")
        data.add_field("type", mime_type)
        data.add_field(
            "file",
            open(Path(file_path), "rb"),  # noqa: SIM115
            filename=Path(file_path).name,
            content_type=mime_type,
        )
        result = await self._request("POST", url, data=data)
        return result["id"]

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Download media by ID. Returns (content, mime_type)."""
        # First, get the media URL
        url = f"{BASE_URL}/{media_id}"
        meta = await self._request("GET", url)
        media_url = meta["url"]

        # Then download the actual file
        if not self._session:
            raise RuntimeError("MetaClient session not started, call start() first")

        async with self._session.get(media_url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise MetaApiError(resp.status, body)
            content = await resp.read()
            mime_type = resp.headers.get("Content-Type", "application/octet-stream")
            return content, mime_type
