"""WhatsApp Meta Cloud API Channel Adapter.

Implements BaseChannel for the Meta WhatsApp Cloud API (graph.facebook.com).
"""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import time
import uuid
from pathlib import Path

from config.logging_config import logger
from core.interfaces.channel import BaseChannel, Message, UserInfo
from ui.secrets_manager import secrets_manager

from .formatting import markdown_to_whatsapp, split_message
from .meta_client import MetaClient
from .models import AuthorizedNumber

WHATSAPP_MAX_MESSAGE_LENGTH = 4096
RATE_LIMIT_WINDOW = 3600  # 1 hour in seconds
RATE_LIMIT_MAX = 240  # max messages per window
SEND_SEMAPHORE_LIMIT = 5  # concurrent sends
SEND_CHUNK_DELAY = 0.5  # delay between message chunks
DEDUP_TTL = 60.0  # seconds to remember message IDs
DEDUP_CLEANUP_INTERVAL = 30.0  # seconds between dedup cleanup runs


class WhatsAppMetaChannel(BaseChannel):
    """WhatsApp messaging channel via Meta Cloud API."""

    platform = "whatsapp"

    # Registry delegated to module-level dict to avoid class-identity
    # issues when the module is imported from different paths
    # (main.py vs ui.app load different class objects).

    @staticmethod
    def register(phone_number_id: str, instance: WhatsAppMetaChannel) -> None:
        from . import registry

        registry.register(phone_number_id, instance)

    @staticmethod
    def unregister(phone_number_id: str) -> None:
        from . import registry

        registry.unregister(phone_number_id)

    @staticmethod
    def get_channel(phone_number_id: str) -> WhatsAppMetaChannel | None:
        from . import registry

        return registry.get_channel(phone_number_id)

    def __init__(self, config: dict, agent_name: str | None = None):
        super().__init__(config, agent_name)
        self.phone_number_id = config.get("phone_number_id", "")
        token_key = config.get("access_token_secret", "WHATSAPP_API_ACCESS_TOKEN")
        self._access_token = secrets_manager.get_plain(token_key) or ""

        self._client: MetaClient | None = None
        self._seen_messages: dict[str, float] = {}
        self._dedup_cleanup_task: asyncio.Task | None = None

        # Rate limiting: sliding window
        self._send_times: list[float] = []
        self._send_semaphore = asyncio.Semaphore(SEND_SEMAPHORE_LIMIT)

    # -- Lifecycle --

    async def start(self) -> None:
        """Start the channel: create client, register, start background tasks."""
        if not self._access_token:
            logger.warning(
                "WhatsApp Meta: no access token configured, channel disabled"
            )
            return

        if not self.phone_number_id:
            logger.warning(
                "WhatsApp Meta: no phone_number_id configured, channel disabled"
            )
            return

        self._client = MetaClient(self.phone_number_id, self._access_token)
        await self._client.start()

        self.register(self.phone_number_id, self)

        # Start dedup cleanup loop
        self._dedup_cleanup_task = asyncio.create_task(self._dedup_cleanup_loop())

        # Warn if no authorized numbers are configured
        authorized = AuthorizedNumber.get_authorized(self.phone_number_id)
        if not authorized:
            logger.warning(
                "WhatsApp Meta (%s): no authorized numbers configured, "
                "running in open mode",
                self.phone_number_id,
            )

        logger.info(
            "WhatsApp Meta channel started (phone_number_id=%s, agent=%s)",
            self.phone_number_id,
            self.agent_name or "default",
        )

    async def stop(self) -> None:
        """Stop the channel: unregister, cancel tasks, close client."""
        self.unregister(self.phone_number_id)

        if self._dedup_cleanup_task and not self._dedup_cleanup_task.done():
            self._dedup_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dedup_cleanup_task
            self._dedup_cleanup_task = None

        if self._client:
            await self._client.close()
            self._client = None

        logger.info(
            "WhatsApp Meta channel stopped (phone_number_id=%s)",
            self.phone_number_id,
        )

    # -- User info --

    async def get_user_info(self, user_id: int) -> UserInfo | None:
        """Get user info for a phone number."""
        phone = str(user_id)
        label = AuthorizedNumber.get_label(self.phone_number_id, phone)
        return UserInfo(
            user_id=user_id,
            username=phone,
            display_name=label or phone,
            platform="whatsapp",
        )

    # -- Incoming messages --

    async def handle_incoming(self, msg: dict) -> None:
        """Handle an incoming webhook message.

        Called by the webhook handler for each message in the payload.
        """
        msg_id = msg.get("id", "")
        if not msg_id or self._is_duplicate(msg_id):
            return

        phone = msg.get("from", "")
        if not phone:
            return

        if not self._check_authorization(phone):
            logger.debug("WhatsApp Meta: unauthorized message from %s", phone)
            return

        # Mark as read (fire-and-forget)
        if self._client:
            asyncio.create_task(self._safe_mark_read(msg_id))

        msg_type = msg.get("type", "")
        await self._process_message(phone, msg_type, msg)

    async def _safe_mark_read(self, message_id: str) -> None:
        """Mark a message as read, suppressing errors."""
        try:
            if self._client:
                await self._client.mark_read(message_id)
        except Exception as exc:
            logger.debug("WhatsApp Meta: failed to mark read: %s", exc)

    async def _process_message(self, phone: str, msg_type: str, msg: dict) -> None:
        """Process a single incoming message and send the response."""
        if not self._message_handler:
            logger.warning("WhatsApp Meta: no message handler set")
            return

        text = ""
        attachments: list[str] = []

        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")

        elif msg_type in ("image", "video", "document"):
            media_obj = msg.get(msg_type, {})
            media_id = media_obj.get("id", "")
            caption = media_obj.get("caption", "")

            if media_id:
                path = await self._download_and_save(media_id, msg_type)
                if path:
                    attachments.append(path)
                    filename = Path(path).name
                    text = (
                        f"{caption}\n\n[Attachment: {filename}]"
                        if caption
                        else f"[Attachment: {filename}]"
                    )
                elif caption:
                    # Download failed but there is a caption
                    text = caption
                    await self._send_error(phone, "Could not download attachment.")
                else:
                    await self._send_error(phone, "Could not download attachment.")
                    return
            elif caption:
                text = caption

        elif msg_type == "audio":
            audio_obj = msg.get("audio", {})
            media_id = audio_obj.get("id", "")

            if media_id:
                path = await self._download_and_save(media_id, "audio")
                if path:
                    # Try transcription if available
                    transcription = await self._try_transcribe(path)
                    if transcription:
                        text = transcription
                    else:
                        attachments.append(path)
                        text = "[Audio message]"
                else:
                    await self._send_error(phone, "Could not download audio.")
                    return
            else:
                return

        else:
            logger.debug("WhatsApp Meta: unsupported message type '%s'", msg_type)
            return

        if not text and not attachments:
            return

        user_info = await self.get_user_info(int(phone))
        message = Message(
            user_id=int(phone),
            username=phone,
            text=text,
            attachments=attachments,
            platform="whatsapp",
        )

        response = await self._message_handler(message, user_info)
        if response:
            await self.send_message(int(phone), response)

    # -- Sending --

    async def send_message(
        self,
        user_id: int,
        text: str,
        attachments: list[str] | None = None,
    ) -> None:
        """Send a message to a WhatsApp user."""
        if not self._client:
            logger.warning("WhatsApp Meta: client not initialized")
            return

        phone = str(user_id)
        formatted = markdown_to_whatsapp(text)
        chunks = split_message(formatted, max_len=WHATSAPP_MAX_MESSAGE_LENGTH)

        for i, chunk in enumerate(chunks):
            if not self._check_rate_limit():
                logger.warning("WhatsApp Meta: rate limit reached, dropping message")
                break

            async with self._send_semaphore:
                try:
                    await self._client.send_text(phone, chunk)
                    self._send_times.append(time.monotonic())
                except Exception as exc:
                    logger.error(
                        "WhatsApp Meta: failed to send text to %s: %s",
                        phone,
                        exc,
                    )
                    break

            if i < len(chunks) - 1:
                await asyncio.sleep(SEND_CHUNK_DELAY)

        # Send file attachments
        if attachments:
            for file_path in attachments:
                await self.send_file(user_id, file_path)

    async def send_file(
        self,
        user_id: int | str,
        file_path: str,
        caption: str | None = None,
    ) -> bool:
        """Send a file to a WhatsApp user."""
        if not self._client:
            logger.warning("WhatsApp Meta: client not initialized")
            return False

        phone = str(user_id)
        path = Path(file_path)
        if not path.exists():
            logger.warning("WhatsApp Meta: file not found: %s", file_path)
            return False

        mime_type, _ = mimetypes.guess_type(file_path)
        mime_type = mime_type or "application/octet-stream"

        try:
            media_id = await self._client.upload_media(file_path, mime_type)
        except Exception as exc:
            logger.error(
                "WhatsApp Meta: failed to upload media %s: %s",
                file_path,
                exc,
            )
            return False

        try:
            if mime_type.startswith("image/"):
                await self._client.send_image(phone, media_id, caption)
            elif mime_type.startswith("audio/"):
                await self._client.send_audio(phone, media_id)
            else:
                await self._client.send_document(phone, media_id, caption, path.name)
            return True
        except Exception as exc:
            logger.error(
                "WhatsApp Meta: failed to send file to %s: %s",
                phone,
                exc,
            )
            return False

    # -- Helpers --

    async def _download_and_save(self, media_id: str, media_type: str) -> str | None:
        """Download media and save via attachments service (fallback /tmp)."""
        if not self._client:
            return None

        try:
            content, mime_type = await self._client.download_media(media_id)
        except Exception as exc:
            logger.error(
                "WhatsApp Meta: failed to download media %s: %s",
                media_id,
                exc,
            )
            return None

        # Determine extension from mime type
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"{media_type}_{uuid.uuid4().hex}{ext}"

        # Try attachments service first
        attachments_service = (
            self._plugin_manager.get_service("attachments")
            if self._plugin_manager
            else None
        )

        tmp_path = Path(f"/tmp/gridbear_wa_meta_{uuid.uuid4().hex}_{filename}")
        try:
            tmp_path.write_bytes(content)

            if attachments_service:
                try:
                    saved = await attachments_service.save_attachment(
                        str(tmp_path), None, filename
                    )
                    if saved:
                        return str(saved)
                except Exception as exc:
                    logger.debug(
                        "WhatsApp Meta: attachments service save failed: %s",
                        exc,
                    )

            # Fallback: keep in /tmp
            return str(tmp_path)
        except Exception as exc:
            logger.error("WhatsApp Meta: failed to save media: %s", exc)
            tmp_path.unlink(missing_ok=True)
            return None

    async def _try_transcribe(self, audio_path: str) -> str | None:
        """Attempt audio transcription if the service is available."""
        transcription_service = (
            self._plugin_manager.get_service("transcription")
            if self._plugin_manager
            else None
        )
        if not transcription_service:
            return None

        try:
            return await transcription_service.transcribe(audio_path)
        except Exception as exc:
            logger.debug("WhatsApp Meta: transcription failed: %s", exc)
            return None

    async def _send_error(self, phone: str, text: str) -> None:
        """Send an error message to the user."""
        if self._client:
            try:
                await self._client.send_text(phone, text)
            except Exception as exc:
                logger.debug("WhatsApp Meta: failed to send error message: %s", exc)

    def _check_authorization(self, phone: str) -> bool:
        """Check if a phone number is authorized.

        Returns True if open mode (no authorized numbers) or phone is in list.
        """
        authorized = AuthorizedNumber.get_authorized(self.phone_number_id)
        if not authorized:
            return True  # Open mode
        return phone in authorized

    def _is_duplicate(self, message_id: str) -> bool:
        """Check and record a message ID for dedup (60s TTL)."""
        now = time.monotonic()
        if message_id in self._seen_messages:
            return True
        self._seen_messages[message_id] = now
        return False

    def _check_rate_limit(self) -> bool:
        """Check sliding window rate limit (240/hour).

        Returns True if under the limit, False if exceeded.
        """
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW
        self._send_times = [t for t in self._send_times if t > cutoff]
        return len(self._send_times) < RATE_LIMIT_MAX

    async def _dedup_cleanup_loop(self) -> None:
        """Background task to clean expired dedup entries."""
        while True:
            try:
                await asyncio.sleep(DEDUP_CLEANUP_INTERVAL)
                now = time.monotonic()
                expired = [
                    mid
                    for mid, ts in self._seen_messages.items()
                    if now - ts > DEDUP_TTL
                ]
                for mid in expired:
                    del self._seen_messages[mid]
                if expired:
                    logger.debug(
                        "WhatsApp Meta: cleaned %d expired dedup entries",
                        len(expired),
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("WhatsApp Meta: dedup cleanup error: %s", exc)
