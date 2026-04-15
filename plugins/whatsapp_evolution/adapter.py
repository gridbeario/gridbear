"""WhatsApp Channel Plugin.

WhatsApp adapter implementing BaseChannel interface via Evolution API.
"""

import asyncio
import base64
import contextlib
import contextvars
import re
import time
import uuid
from pathlib import Path
from typing import List

from config.logging_config import logger
from config.settings import (
    get_unified_user_id,
    get_user_locale,
)
from core.i18n import make_translator, set_language
from core.interfaces.channel import BaseChannel, Message, UserInfo
from ui.secrets_manager import secrets_manager

from .evolution_client import (
    EvolutionClient,
    EvolutionConnectionError,
    EvolutionNotFoundError,
)
from .formatting import markdown_to_whatsapp, split_message

# Create translator for this plugin
_ = make_translator("whatsapp_evolution")

WHATSAPP_MAX_MESSAGE_LENGTH = 4096
COMMAND_PREFIX = "!"

# Days of week for memo scheduling
DAYS_OF_WEEK = {
    "monday": "MON",
    "tuesday": "TUE",
    "wednesday": "WED",
    "thursday": "THU",
    "friday": "FRI",
    "saturday": "SAT",
    "sunday": "SUN",
}
DAYS_IT = {
    "lunedi": "MON",
    "lunedì": "MON",
    "martedi": "TUE",
    "martedì": "TUE",
    "mercoledi": "WED",
    "mercoledì": "WED",
    "giovedi": "THU",
    "giovedì": "THU",
    "venerdi": "FRI",
    "venerdì": "FRI",
    "sabato": "SAT",
    "domenica": "SUN",
}

# Rate limiting
RATE_LIMIT_WINDOW = 3600  # 1 hour
RATE_LIMIT_MAX = 240  # max messages per window
SEND_SEMAPHORE_LIMIT = 5  # concurrent sends
SEND_CHUNK_DELAY = 0.5  # delay between message chunks

# Dedup
DEDUP_TTL = 60.0  # seconds to remember message IDs

# Contextvar: tracks which EvolutionClient is active per async task
_active_client_var: contextvars.ContextVar[EvolutionClient | None] = (
    contextvars.ContextVar("wa_active_client", default=None)
)


class WhatsAppChannel(BaseChannel):
    """WhatsApp messaging channel via Evolution API."""

    platform = "whatsapp"

    def __init__(self, config: dict):
        super().__init__(config)
        token_env = config.get("token_env", "EVOLUTION_API_KEY")
        self.api_key = secrets_manager.get_plain(token_env)
        self.instance_name = config.get("instance_name", "")
        self.api_url = config.get(
            "evolution_api_url",
            secrets_manager.get_plain(
                "EVOLUTION_API_URL", default="http://gridbear-evolution:8080"
            ),
        )

        self._client: EvolutionClient | None = None
        self._active_tasks: dict[int, asyncio.Task] = {}
        self._seen_messages: dict[str, float] = {}
        self._phone_to_name: dict[int, str] = {}

        # Rate limiting
        self._message_timestamps: list[float] = []
        self._send_semaphore = asyncio.Semaphore(SEND_SEMAPHORE_LIMIT)

        # Dedup cleanup task
        self._dedup_cleanup_task: asyncio.Task | None = None

        # Multi-tenant: per-user instances
        self._user_clients: dict[str, EvolutionClient] = {}
        self._instance_owners: dict[str, str] = {}  # instance_name → unified_id

    @property
    def _active_client(self) -> EvolutionClient | None:
        """Return the contextvar client if set, else the primary client."""
        return _active_client_var.get() or self._client

    def _get_client(self, instance_name: str) -> EvolutionClient | None:
        """Get the EvolutionClient for a given instance name."""
        if instance_name == self.instance_name:
            return self._client
        return self._user_clients.get(instance_name)

    async def add_user_instance(
        self, unified_id: str, instance_name: str
    ) -> EvolutionClient:
        """Register a user instance at runtime (called from portal)."""
        client = EvolutionClient(self.api_url, self.api_key, instance_name)
        await client.start()
        self._user_clients[instance_name] = client
        self._instance_owners[instance_name] = unified_id
        logger.info(
            "WhatsApp: registered user instance '%s' (owner: %s)",
            instance_name,
            unified_id,
        )
        return client

    async def remove_user_instance(self, instance_name: str) -> None:
        """Remove a user instance at runtime."""
        client = self._user_clients.pop(instance_name, None)
        self._instance_owners.pop(instance_name, None)
        if client:
            await client.stop()
        logger.info("WhatsApp: removed user instance '%s'", instance_name)

    def _create_runner_callbacks(self, phone: str):
        """Create progress, error, tool, and stream callbacks for runner.

        WhatsApp doesn't support message editing, so callbacks refresh
        'composing' presence instead.
        """
        active_client = self._active_client

        async def progress_callback(progress_message: str):
            """Refresh composing presence."""
            if active_client:
                await active_client.send_presence("composing", str(phone))

        async def error_callback(error_type: str, details: dict):
            """Handle runner errors with user notification."""
            logger.error(f"Runner error [{error_type}]: {details}")
            try:
                if error_type == "timeout":
                    timeout_secs = details.get("timeout_seconds", "?")
                    await self._send_text(
                        phone,
                        f"Timeout dopo {timeout_secs} secondi. "
                        "La richiesta era troppo complessa. Riprova semplificando.",
                    )
                elif error_type == "exception":
                    error_msg = details.get("error", "Errore sconosciuto")
                    await self._send_text(
                        phone, f"Si è verificato un errore: {error_msg[:100]}"
                    )
                elif error_type == "retries_exhausted":
                    await self._send_text(
                        phone, "Tutti i tentativi falliti. Riprova più tardi."
                    )
            except Exception as e:
                logger.warning(f"Failed to send error notification: {e}")

        async def tool_callback(tool_name: str, tool_input: dict):
            """Refresh composing presence on tool use."""
            if active_client:
                await active_client.send_presence("composing", str(phone))

        async def stream_callback(text: str):
            """Refresh composing presence on stream."""
            if active_client:
                await active_client.send_presence("composing", str(phone))

        return progress_callback, error_callback, tool_callback, stream_callback

    def _set_user_language(self, username: str | None) -> None:
        """Set translation language based on user's locale preference."""
        if username:
            unified_id = get_unified_user_id("whatsapp", username)
            locale = get_user_locale(unified_id)
            set_language(locale)
        else:
            set_language("en")

    async def initialize(self) -> None:
        """Initialize WhatsApp channel."""
        if not self.api_key:
            logger.warning("WhatsApp: Evolution API key not configured")
            return
        logger.info(f"WhatsApp channel initialized (instance: {self.instance_name})")

    async def start(self) -> None:
        """Start WhatsApp channel: create client, ensure instance exists."""
        if not self.api_key:
            logger.warning("Cannot start WhatsApp: no API key configured")
            return

        if not self.instance_name:
            self.instance_name = self.agent_name or "gridbear"
            logger.info(f"WhatsApp: using instance name '{self.instance_name}'")

        self._client = EvolutionClient(self.api_url, self.api_key, self.instance_name)
        await self._client.start()

        # Try to check connection status; instance may not exist yet
        try:
            status = await self._client.get_connection_status()
            state = (
                status.get("instance", {}).get("state", "unknown")
                if status
                else "unknown"
            )
            logger.info(f"WhatsApp instance '{self.instance_name}' state: {state}")
        except EvolutionNotFoundError:
            logger.info(
                f"WhatsApp instance '{self.instance_name}' not found — will be created via admin UI"
            )
        except EvolutionConnectionError as e:
            logger.warning(f"WhatsApp: cannot reach Evolution API: {e}")
        except Exception as e:
            logger.warning(f"WhatsApp: error checking instance: {e}")

        # Start dedup cleanup
        self._dedup_cleanup_task = asyncio.create_task(self._dedup_cleanup_loop())

        # Load user instances from DB
        try:
            from .models import UserInstance

            for inst in await UserInstance.search(
                [("agent_name", "=", self.agent_name)], order="created_at"
            ):
                try:
                    client = EvolutionClient(
                        self.api_url, self.api_key, inst["instance_name"]
                    )
                    await client.start()
                    self._user_clients[inst["instance_name"]] = client
                    self._instance_owners[inst["instance_name"]] = inst["unified_id"]
                    logger.info(
                        "WhatsApp: loaded user instance '%s'", inst["instance_name"]
                    )
                except Exception as e:
                    logger.warning(
                        "WhatsApp: failed to load user instance '%s': %s",
                        inst["instance_name"],
                        e,
                    )
        except Exception as e:
            logger.debug("WhatsApp: skipping user instance load: %s", e)

        logger.info("WhatsApp channel started")

    async def stop(self) -> None:
        """Stop WhatsApp channel. Does NOT disconnect WhatsApp session."""
        # Cancel active tasks
        for phone, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
        self._active_tasks.clear()

        # Cancel dedup cleanup
        if self._dedup_cleanup_task and not self._dedup_cleanup_task.done():
            self._dedup_cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dedup_cleanup_task

        if self._client:
            await self._client.stop()
            self._client = None

        # Cleanup user clients
        for name, client in list(self._user_clients.items()):
            try:
                await client.stop()
            except Exception:
                pass
        self._user_clients.clear()
        self._instance_owners.clear()

        logger.info("WhatsApp channel stopped")

    async def send_message(
        self,
        user_id: int,
        text: str,
        attachments: list[str] | None = None,
    ) -> None:
        """Send message to a user."""
        client = self._active_client
        if not client:
            return

        phone = str(user_id)
        formatted_text = markdown_to_whatsapp(text)
        chunks = split_message(formatted_text, WHATSAPP_MAX_MESSAGE_LENGTH)

        for chunk in chunks:
            if not self._check_rate_limit():
                logger.warning("WhatsApp rate limit reached, dropping message")
                return
            async with self._send_semaphore:
                try:
                    await client.send_text(phone, chunk)
                except Exception as e:
                    logger.error(f"Failed to send WhatsApp message: {e}")
            if len(chunks) > 1:
                await asyncio.sleep(SEND_CHUNK_DELAY)

        if attachments:
            for attachment_path in attachments:
                await self.send_file(user_id, attachment_path)

    async def send_file(
        self,
        user_id: int | str,
        file_path: str,
        caption: str | None = None,
    ) -> bool:
        """Send a file to a chat."""
        client = self._active_client
        if not client:
            return False

        phone = str(user_id)
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"File not found: {file_path}")
            return False

        try:
            suffix = path.suffix.lower()
            if suffix in [".ogg", ".opus", ".mp3", ".m4a", ".wav"]:
                await client.send_audio(phone, str(path))
            elif suffix in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                await client.send_media(phone, "image", str(path), caption)
            elif suffix in [".mp4", ".avi", ".mov", ".mkv"]:
                await client.send_media(phone, "video", str(path), caption)
            else:
                await client.send_media(phone, "document", str(path), caption)
            logger.info(f"Sent file {file_path} to {phone}")
            return True
        except Exception as e:
            logger.error(f"Failed to send file {file_path}: {e}")
            return False

    async def get_user_info(self, user_id: int) -> UserInfo | None:
        """Get user information."""
        name = self._phone_to_name.get(user_id)
        phone_str = str(user_id)
        return UserInfo(
            user_id=user_id,
            username=phone_str,
            display_name=name or phone_str,
            platform="whatsapp",
        )

    # --- Internal send helper ---

    async def _send_text(self, phone: str | int, text: str) -> None:
        """Internal helper to send a text message."""
        client = self._active_client
        if not client:
            return
        phone_str = str(phone)
        formatted = markdown_to_whatsapp(text)
        chunks = split_message(formatted, WHATSAPP_MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            if not self._check_rate_limit():
                return
            async with self._send_semaphore:
                try:
                    await client.send_text(phone_str, chunk)
                except Exception as e:
                    logger.error(f"Failed to send text to {phone_str}: {e}")
            if len(chunks) > 1:
                await asyncio.sleep(SEND_CHUNK_DELAY)

    # --- Webhook Handler ---

    async def _handle_webhook(self, payload: dict, instance_name: str = "") -> None:
        """Route incoming webhook events."""
        event = payload.get("event")

        if event == "messages.upsert":
            await self._handle_message(
                payload.get("data", {}), instance_name=instance_name
            )
        elif event == "connection.update":
            self._handle_connection_update(payload.get("data", {}))
        else:
            logger.debug(f"WhatsApp webhook: unhandled event '{event}'")

    def _handle_connection_update(self, data: dict) -> None:
        """Handle connection status changes."""
        state = data.get("state", "unknown")
        logger.info(f"WhatsApp connection update: {state}")
        if state == "close":
            logger.warning(
                f"WhatsApp instance '{self.instance_name}' disconnected. "
                "Reconnection requires QR scan via admin UI."
            )

    async def _handle_message(self, data: dict, instance_name: str = "") -> None:
        """Process incoming WhatsApp message."""
        try:
            key = data.get("key", {})
            message_content = data.get("message", {})

            # Skip messages sent by us
            if key.get("fromMe"):
                return

            # Get message ID for dedup
            message_id = key.get("id", "")
            if self._is_duplicate(message_id):
                return

            # Get sender info
            remote_jid = key.get("remoteJid", "")

            # Skip group messages in Phase 1 (Phase 4 adds group support)
            is_group = remote_jid.endswith("@g.us")

            # Extract phone number
            if is_group:
                participant = key.get("participant", "")
                phone_str = participant.split("@")[0] if participant else ""
            else:
                phone_str = remote_jid.split("@")[0] if remote_jid else ""

            if not phone_str:
                return

            phone = int(phone_str)

            # Cache push name
            push_name = data.get("pushName", "")
            if push_name:
                self._phone_to_name[phone] = push_name

            username = phone_str
            self._set_user_language(username)

            # Set active client for this async task
            resolved_instance = instance_name or self.instance_name
            client = self._get_client(resolved_instance)
            _active_client_var.set(client)

            # Extract text content (before auth check for wake word matching)
            text = (
                message_content.get("conversation")
                or message_content.get("extendedTextMessage", {}).get("text")
                or ""
            )

            # Authorization check (diverges for user vs YAML instances)
            is_user_instance = resolved_instance in self._user_clients
            if is_user_instance:
                from .models import UserInstance, WakeWord

                auth_result = await UserInstance.check_phone_auth(
                    resolved_instance, phone_str
                )
                if not auth_result["authorized"]:
                    # Check wake words before rejecting
                    wake_response = await WakeWord.check_wake_words(
                        resolved_instance, text
                    )
                    if wake_response:
                        await self._send_text(phone_str, wake_response)
                        return
                    if not auth_result["silent_reject"]:
                        msg = auth_result["reject_message"] or _("Not authorized.")
                        await self._send_text(phone_str, msg)
                    return
            else:
                if not self.is_authorized(phone, username):
                    await self._send_text(phone_str, _("Not authorized."))
                    return

            # Handle media messages (Phase 2)
            has_audio = "audioMessage" in message_content
            has_image = "imageMessage" in message_content
            has_video = "videoMessage" in message_content
            has_document = "documentMessage" in message_content
            has_media = has_audio or has_image or has_video or has_document

            # Get caption from media messages
            if not text and has_media:
                for media_key in ["imageMessage", "videoMessage", "documentMessage"]:
                    if media_key in message_content:
                        text = message_content[media_key].get("caption", "")
                        break

            # Voice messages
            if has_audio:
                await self._handle_voice(phone, phone_str, push_name, data, is_group)
                return

            # Media attachments (non-audio)
            if has_media and not has_audio:
                await self._handle_media_attachment(
                    phone, phone_str, push_name, text, data, is_group
                )
                return

            if not text:
                return

            # Group chat: respond only if mentioned or trigger word
            if is_group:
                agent_ctx = self.get_agent_context()
                agent_name = agent_ctx.get("display_name", "gridbear").lower()
                text_lower = text.lower()
                if agent_name not in text_lower and "gridbear" not in text_lower:
                    return

            # Check for commands (!prefix)
            if text.startswith(COMMAND_PREFIX):
                await self._handle_command(phone, phone_str, push_name, text, is_group)
                return

            # Process regular message
            if self._message_handler:
                message = Message(
                    user_id=phone,
                    username=username,
                    text=text,
                    platform="whatsapp",
                    is_group_chat=is_group,
                )
                user_info = UserInfo(
                    user_id=phone,
                    username=username,
                    display_name=push_name or phone_str,
                    platform="whatsapp",
                )

                # Store user message in chat history
                sessions_service = (
                    self._plugin_manager.get_service("sessions")
                    if self._plugin_manager
                    else None
                )
                if sessions_service:
                    await sessions_service.store_chat_message(
                        user_id=phone,
                        platform="whatsapp",
                        role="user",
                        content=text,
                        username=username,
                    )

                # Reject if already processing
                if phone in self._active_tasks and not self._active_tasks[phone].done():
                    await self._send_text(
                        phone_str,
                        _("A request is already running. Use !stop to cancel it."),
                    )
                    return

                task = asyncio.create_task(
                    self._run_message_pipeline(message, user_info, phone, phone_str)
                )
                self._active_tasks[phone] = task

                try:
                    await task
                except asyncio.CancelledError:
                    await self._send_text(phone_str, _("Request cancelled."))
                finally:
                    self._active_tasks.pop(phone, None)

        except Exception as e:
            logger.exception(f"Error handling WhatsApp message: {e}")

    async def _run_message_pipeline(
        self,
        message: Message,
        user_info: UserInfo,
        phone: int,
        phone_str: str,
    ) -> None:
        """Execute the full message processing pipeline."""
        progress_cb, error_cb, tool_cb, stream_cb = self._create_runner_callbacks(
            phone_str
        )

        # Show composing presence
        active = self._active_client
        if active:
            await active.send_presence("composing", phone_str)

        try:
            response_text = await self._message_handler(
                message,
                user_info,
                progress_callback=progress_cb,
                error_callback=error_cb,
                tool_callback=tool_cb,
                stream_callback=stream_cb,
            )

            # Extract and create memos
            if self._task_scheduler:
                memos = self._extract_memos(response_text)
                for memo in memos:
                    await self._create_memo_from_tag(phone, memo)
                response_text = self._remove_memo_tags(response_text)

            # Extract and send files
            files_to_send = self._extract_file_paths(response_text)
            response_text = self._remove_file_tags(response_text)

            # Store assistant response in chat history
            sessions_service = (
                self._plugin_manager.get_service("sessions")
                if self._plugin_manager
                else None
            )
            if sessions_service and response_text.strip():
                await sessions_service.store_chat_message(
                    user_id=phone,
                    platform="whatsapp",
                    role="assistant",
                    content=response_text,
                    username=message.username,
                )

            # Send response
            if response_text.strip():
                await self._send_text(phone_str, response_text)

            # Send files
            for file_path in files_to_send:
                await self.send_file(phone, file_path)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await self._send_text(
                phone_str,
                _("Error: {error}").format(error=str(e)),
            )

    # --- Voice Messages (Phase 2) ---

    async def _handle_voice(
        self,
        phone: int,
        phone_str: str,
        push_name: str,
        data: dict,
        is_group: bool,
    ) -> None:
        """Handle voice messages: transcribe, process, respond with voice."""
        username = phone_str
        self._set_user_language(username)

        # Reject if already processing
        if phone in self._active_tasks and not self._active_tasks[phone].done():
            await self._send_text(
                phone_str,
                _("A request is already running. Use !stop to cancel it."),
            )
            return

        voice_service = (
            self._plugin_manager.get_service("transcription")
            if self._plugin_manager
            else None
        )
        if not voice_service:
            await self._send_text(phone_str, _("Voice service not available."))
            return

        # Download audio
        voice_path = Path(f"/tmp/gridbear_wa_voice_{uuid.uuid4().hex}.ogg")
        try:
            client = self._active_client
            media_b64 = await client.download_media(data.get("message", {}))
            if not media_b64:
                await self._send_text(phone_str, _("Could not download voice message."))
                return

            voice_path.write_bytes(base64.b64decode(media_b64))

            # Transcribe
            await self._send_text(phone_str, _("Transcribing..."))
            text = await voice_service.transcribe(str(voice_path))

            if not text:
                await self._send_text(
                    phone_str, _("Could not transcribe voice message.")
                )
                return

            # Show transcription in italic
            await self._send_text(phone_str, f"_{text}_")

            # In groups, check if mentioned
            if is_group:
                agent_ctx = self.get_agent_context()
                agent_name = agent_ctx.get("display_name", "gridbear").lower()
                text_lower = text.lower()
                if agent_name not in text_lower and "gridbear" not in text_lower:
                    return

            if not self._message_handler:
                return

            message = Message(
                user_id=phone,
                username=username,
                text=text,
                platform="whatsapp",
                respond_with_voice=True,
                is_group_chat=is_group,
            )
            user_info = UserInfo(
                user_id=phone,
                username=username,
                display_name=push_name or phone_str,
                platform="whatsapp",
            )

            progress_cb, error_cb, tool_cb, stream_cb = self._create_runner_callbacks(
                phone_str
            )

            try:
                response_text = await self._message_handler(
                    message,
                    user_info,
                    progress_callback=progress_cb,
                    error_callback=error_cb,
                    tool_callback=tool_cb,
                    stream_callback=stream_cb,
                )

                # Try voice response
                tts_service = (
                    self._plugin_manager.get_service("tts")
                    if self._plugin_manager
                    else None
                )
                if tts_service and response_text.strip():
                    agent_ctx = self.get_agent_context()
                    voice_config = agent_ctx.get("voice", {})
                    voice_id = voice_config.get("voice_id")

                    unified_id = (
                        get_unified_user_id("whatsapp", username) if username else None
                    )
                    locale = get_user_locale(unified_id) if unified_id else "en"

                    # Clean text for TTS
                    clean_text = response_text
                    clean_text = self._remove_file_tags(clean_text)

                    if clean_text.strip():
                        try:
                            if voice_id:
                                audio_path = await tts_service.synthesize(
                                    clean_text.strip(), voice=voice_id
                                )
                            else:
                                audio_path = await tts_service.synthesize(
                                    clean_text.strip(), locale=locale
                                )
                            await client.send_audio(phone_str, audio_path)
                            Path(audio_path).unlink(missing_ok=True)
                        except Exception as e:
                            logger.warning(f"TTS failed, falling back to text: {e}")
                            await self._send_text(phone_str, response_text)
                    else:
                        await self._send_text(phone_str, response_text)
                else:
                    await self._send_text(phone_str, response_text)

                # Memos
                if self._task_scheduler:
                    memos = self._extract_memos(response_text)
                    for memo in memos:
                        await self._create_memo_from_tag(phone, memo)

                # Files
                files_to_send = self._extract_file_paths(response_text)
                for file_path in files_to_send:
                    await self.send_file(phone, file_path)

            except Exception as e:
                logger.exception(f"Error processing voice message: {e}")
                await self._send_text(
                    phone_str,
                    _("Error: {error}").format(error=str(e)),
                )

        finally:
            voice_path.unlink(missing_ok=True)

    # --- Media Attachments (Phase 2) ---

    async def _handle_media_attachment(
        self,
        phone: int,
        phone_str: str,
        push_name: str,
        caption: str,
        data: dict,
        is_group: bool,
    ) -> None:
        """Handle image/video/document attachments."""
        username = phone_str
        self._set_user_language(username)

        # Reject if already processing
        if phone in self._active_tasks and not self._active_tasks[phone].done():
            await self._send_text(
                phone_str,
                _("A request is already running. Use !stop to cancel it."),
            )
            return

        sessions_service = (
            self._plugin_manager.get_service("sessions")
            if self._plugin_manager
            else None
        )
        attachments_service = (
            self._plugin_manager.get_service("attachments")
            if self._plugin_manager
            else None
        )

        if not sessions_service or not attachments_service:
            await self._send_text(phone_str, _("Services not available."))
            return

        session = await sessions_service.get_session(phone, "whatsapp")
        if not session:
            session = await sessions_service.create_session(phone, "whatsapp")

        # Download media
        message_content = data.get("message", {})
        media_b64 = await self._active_client.download_media(message_content)
        if not media_b64:
            await self._send_text(phone_str, _("Could not download attachment."))
            return

        # Determine filename and save
        filename = "file"
        for media_key, default_name in [
            ("imageMessage", "image.jpg"),
            ("videoMessage", "video.mp4"),
            ("documentMessage", "document"),
        ]:
            if media_key in message_content:
                msg_data = message_content[media_key]
                filename = msg_data.get("fileName", default_name)
                if media_key == "documentMessage" and filename == "document":
                    mimetype = msg_data.get("mimetype", "")
                    ext = mimetype.split("/")[-1] if "/" in mimetype else "bin"
                    filename = f"document.{ext}"
                break

        # Save to temp and store as attachment
        tmp_path = Path(f"/tmp/gridbear_wa_attach_{uuid.uuid4().hex}_{filename}")
        try:
            tmp_path.write_bytes(base64.b64decode(media_b64))
            attachment_path = await attachments_service.save_attachment(
                str(tmp_path), session.id, filename
            )
        finally:
            tmp_path.unlink(missing_ok=True)

        if attachment_path and self._message_handler:
            attachment_tag = _("[Attachment: {filename}]").format(filename=filename)
            text = f"{caption}\n\n{attachment_tag}" if caption else attachment_tag

            message = Message(
                user_id=phone,
                username=username,
                text=text,
                attachments=[str(attachment_path)],
                platform="whatsapp",
                is_group_chat=is_group,
            )
            user_info = UserInfo(
                user_id=phone,
                username=username,
                display_name=push_name or phone_str,
                platform="whatsapp",
            )

            progress_cb, error_cb, tool_cb, stream_cb = self._create_runner_callbacks(
                phone_str
            )
            try:
                response_text = await self._message_handler(
                    message,
                    user_info,
                    progress_callback=progress_cb,
                    error_callback=error_cb,
                    tool_callback=tool_cb,
                    stream_callback=stream_cb,
                )

                if response_text.strip():
                    await self._send_text(phone_str, response_text)
            except Exception as e:
                logger.exception(f"Error processing attachment: {e}")
                await self._send_text(
                    phone_str,
                    _("Error: {error}").format(error=str(e)),
                )

    # --- Commands ---

    async def _handle_command(
        self,
        phone: int,
        phone_str: str,
        push_name: str,
        text: str,
        is_group: bool,
    ) -> None:
        """Handle !commands."""
        parts = text[len(COMMAND_PREFIX) :].strip().split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        args_str = parts[1] if len(parts) > 1 else ""
        args = args_str.split() if args_str else []

        if cmd == "start":
            await self._cmd_start(phone, phone_str)
        elif cmd == "help":
            await self._cmd_help(phone_str)
        elif cmd == "reset":
            await self._cmd_reset(phone, phone_str)
        elif cmd == "stop":
            await self._cmd_stop(phone, phone_str)
        elif cmd == "memo":
            await self._cmd_tasks(phone, phone_str)
        elif cmd == "delmemo":
            await self._cmd_deltask(phone, phone_str, args)
        elif cmd == "memory":
            await self._cmd_memory(phone, phone_str, args)
        else:
            await self._send_text(
                phone_str, _("Unknown command. Use !help for a list.")
            )

    async def _cmd_start(self, phone: int, phone_str: str) -> None:
        await self._send_text(
            phone_str,
            _(
                "Hello! I'm your GridBear assistant.\n"
                "Write anything and I'll respond.\n"
                "Use !reset to start a new conversation."
            ),
        )

    async def _cmd_help(self, phone_str: str) -> None:
        await self._send_text(
            phone_str,
            _(
                "*GridBear Commands*\n\n"
                "*Chat:*\n"
                "!start - Welcome message\n"
                "!reset - Reset session\n"
                "!help - Show this message\n"
                "!stop - Stop current request\n\n"
                "*Memory:*\n"
                "!memory - Memory management\n\n"
                "*Memos/Reminders:*\n"
                "!memo - List memos\n"
                "!delmemo <id> - Delete memo\n"
                "!delmemo all - Delete all\n\n"
                "*Creating memos:*\n"
                '"gridbear remind me every Monday at 9 to X"\n'
                '"gridbear in 30 minutes tell me Y"\n\n'
                "You can also send voice notes and attachments."
            ),
        )

    async def _cmd_reset(self, phone: int, phone_str: str) -> None:
        sessions_service = (
            self._plugin_manager.get_service("sessions")
            if self._plugin_manager
            else None
        )
        attachments_service = (
            self._plugin_manager.get_service("attachments")
            if self._plugin_manager
            else None
        )

        if sessions_service:
            session_ids = await sessions_service.clear_session(phone, "whatsapp")
            if attachments_service:
                for sid in session_ids:
                    await attachments_service.cleanup_session(sid)

        await self._send_text(phone_str, _("Session reset. Start a new chat!"))

    async def _cmd_stop(self, phone: int, phone_str: str) -> None:
        task = self._active_tasks.get(phone)
        if not task or task.done():
            await self._send_text(phone_str, _("No active request to stop."))
            return

        task.cancel()

        from core.mcp_gateway.server import get_task_manager

        task_manager = get_task_manager()
        if task_manager and self.agent_name:
            cancelled = task_manager.cancel_tasks_by_agent(self.agent_name)
            if cancelled:
                logger.info(
                    "Cancelled %d async tasks for agent %s", cancelled, self.agent_name
                )

        await self._send_text(phone_str, _("Stopping..."))

    async def _cmd_tasks(self, phone: int, phone_str: str) -> None:
        """List memos for user."""
        if not self._task_scheduler:
            await self._send_text(phone_str, _("Memo service not available."))
            return

        tasks = await self._task_scheduler.list_memos(phone, "whatsapp")
        if not tasks:
            await self._send_text(phone_str, _("No memos."))
            return

        unified_id = get_unified_user_id("whatsapp", phone_str)
        set_language(get_user_locale(unified_id) if unified_id else "en")

        lines = [_("*Your memos:*\n")]
        for t in tasks:
            status = "✅" if t["enabled"] else "⏸️"
            if t.get("schedule_type") == "cron" and t.get("cron"):
                when = t["cron"]
            elif t.get("run_at"):
                try:
                    from datetime import datetime

                    run_at = datetime.fromisoformat(t["run_at"])
                    when = run_at.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    when = t["run_at"]
            else:
                when = t.get("description", "")

            title = t.get("prompt_title", "")
            content = t.get("prompt_content", "") or t.get("prompt", "")
            display_text = (
                title
                if title
                else (content[:80] + "..." if len(content) > 80 else content)
            )
            lines.append(f"{status} *{t['id']}*: {display_text}")
            lines.append(f"   ⏰ {when}")
            if t.get("last_run"):
                lines.append(f"   {_('Last run:')} {t['last_run'][:16]}")

        await self._send_text(phone_str, "\n".join(lines))

    async def _cmd_deltask(self, phone: int, phone_str: str, args: list) -> None:
        """Delete a memo."""
        if not self._task_scheduler:
            await self._send_text(phone_str, _("Memo service not available."))
            return

        if not args:
            await self._send_text(phone_str, _("Usage: !delmemo <id> or !delmemo all"))
            return

        if args[0].lower() == "all":
            memos = await self._task_scheduler.list_memos(phone, "whatsapp")
            count = 0
            for m in memos:
                if await self._task_scheduler.delete_memo(m["id"], phone):
                    count += 1
            await self._send_text(
                phone_str,
                _("Deleted {count} memo(s).").format(count=count),
            )
        else:
            try:
                memo_id = int(args[0])
                if await self._task_scheduler.delete_memo(memo_id, phone):
                    await self._send_text(
                        phone_str,
                        _("Memo {memo_id} deleted.").format(memo_id=memo_id),
                    )
                else:
                    await self._send_text(phone_str, _("Memo not found."))
            except ValueError:
                await self._send_text(phone_str, _("Invalid ID."))

    async def _cmd_memory(self, phone: int, phone_str: str, args: list) -> None:
        """Memory management command."""
        unified_id = get_unified_user_id("whatsapp", phone_str)

        memory_service = (
            self._plugin_manager.get_service("memory") if self._plugin_manager else None
        )
        if not memory_service or not memory_service.enabled:
            await self._send_text(phone_str, _("Memory service not available."))
            return

        if not args:
            await self._send_text(
                phone_str,
                _(
                    "*Memory Management*\n\n"
                    "!memory stats - Statistics\n"
                    "!memory list [n] - Recent memories\n"
                    "!memory search <query> - Search\n"
                    "!memory delete <id> - Delete one\n"
                    "!memory clear <type> - Clear by type"
                ),
            )
            return

        subcommand = args[0].lower()

        if subcommand == "stats":
            stats = await memory_service.get_stats(phone_str)
            if stats:
                await self._send_text(
                    phone_str,
                    _(
                        "*Memory Stats:*\n"
                        "Total: {total}\n"
                        "Episodic: {episodic}\n"
                        "Facts: {facts}"
                    ).format(
                        total=stats.get("total", 0),
                        episodic=stats.get("episodic", 0),
                        facts=stats.get("facts", 0),
                    ),
                )
            else:
                await self._send_text(phone_str, _("No memory stats available."))

        elif subcommand == "list":
            limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
            memories = await memory_service.list_memories(phone_str, limit=limit)
            if memories:
                lines = [_("*Recent memories:*\n")]
                for m in memories:
                    content = m.get("content", "")[:80]
                    lines.append(f"• {m.get('id', '?')}: {content}")
                await self._send_text(phone_str, "\n".join(lines))
            else:
                await self._send_text(phone_str, _("No memories found."))

        elif subcommand == "search":
            if len(args) < 2:
                await self._send_text(phone_str, _("Usage: !memory search <query>"))
                return
            query = " ".join(args[1:])
            results = await memory_service.search(unified_id, query)
            if results:
                lines = [_("*Search results:*\n")]
                for r in results[:5]:
                    content = r.get("content", "")[:80]
                    lines.append(f"• {content}")
                await self._send_text(phone_str, "\n".join(lines))
            else:
                await self._send_text(phone_str, _("No results."))

        elif subcommand == "delete":
            if len(args) < 2:
                await self._send_text(phone_str, _("Usage: !memory delete <id>"))
                return
            try:
                await memory_service.delete(args[1])
                await self._send_text(phone_str, _("Memory deleted."))
            except Exception as e:
                await self._send_text(phone_str, f"Error: {e}")

        elif subcommand == "clear":
            if len(args) < 2:
                await self._send_text(
                    phone_str,
                    _("Usage: !memory clear <type>\n\nTypes: all, episodic, facts"),
                )
                return
            mode = args[1].lower()
            type_map = {"all": None, "episodic": "episodic", "facts": "declarative"}
            if mode not in type_map:
                await self._send_text(
                    phone_str,
                    _("Invalid type. Use: all, episodic, facts"),
                )
                return
            try:
                memory_service.clear_user_memories(
                    "whatsapp",
                    phone_str,
                    memory_type=type_map[mode],
                )
                await self._send_text(
                    phone_str,
                    _("Memories cleared (type: {mode}).").format(mode=mode),
                )
            except Exception as e:
                await self._send_text(phone_str, f"Error: {e}")

    # --- Scheduled Tasks ---

    async def execute_scheduled_task(self, user_id: int, prompt: str) -> None:
        """Execute a scheduled task and send result to user."""
        if not self._client or not self._message_handler:
            logger.error(
                "Cannot execute scheduled task: client or handler not initialized"
            )
            return

        phone_str = str(user_id)
        username = phone_str
        message = Message(
            user_id=user_id,
            username=username,
            text=prompt,
            platform="whatsapp",
        )
        user_info = UserInfo(
            user_id=user_id,
            username=username,
            display_name="Scheduled",
            platform="whatsapp",
        )

        try:
            response_text = await self._message_handler(
                message,
                user_info,
                progress_callback=None,
                error_callback=None,
            )

            # Extract and create any follow-up memos
            if self._task_scheduler:
                memos = self._extract_memos(response_text)
                for memo in memos:
                    await self._create_memo_from_tag(user_id, memo)
                response_text = self._remove_memo_tags(response_text)

            # Extract and send files
            files_to_send = self._extract_file_paths(response_text)
            response_text = self._remove_file_tags(response_text)

            formatted_text = f"*Memo*\n\n{response_text}"
            await self._send_text(phone_str, formatted_text)

            for file_path in files_to_send:
                await self.send_file(user_id, file_path)

        except Exception as e:
            logger.exception(f"Error executing scheduled task: {e}")

    # --- Memo/File Tag Extraction ---

    @staticmethod
    def _extract_memos(text: str) -> List[dict]:
        """Extract [CREATE_MEMO: type | schedule | title | task] tags from text."""
        memos = []
        pattern_4 = (
            r"\[CREATE_MEMO:\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^\]]+)\]"
        )
        matches = re.findall(pattern_4, text, re.IGNORECASE)
        for match in matches:
            memos.append(
                {
                    "type": match[0].strip().lower(),
                    "schedule": match[1].strip(),
                    "title": match[2].strip(),
                    "task": match[3].strip(),
                }
            )

        if not memos:
            pattern_3 = r"\[CREATE_MEMO:\s*([^|]+)\s*\|\s*([^|]+)\s*\|\s*([^\]]+)\]"
            matches = re.findall(pattern_3, text, re.IGNORECASE)
            for match in matches:
                task = match[2].strip()
                memos.append(
                    {
                        "type": match[0].strip().lower(),
                        "schedule": match[1].strip(),
                        "title": task[:50],
                        "task": task,
                    }
                )
        return memos

    @staticmethod
    def _remove_memo_tags(text: str) -> str:
        """Remove [CREATE_MEMO: ...] tags from text."""
        return re.sub(r"\[CREATE_MEMO:[^\]]+\]", "", text, flags=re.IGNORECASE).strip()

    async def _create_memo_from_tag(self, user_id: int, memo: dict) -> int | None:
        """Create a memo/scheduled task from parsed tag."""
        if not self._task_scheduler:
            return None

        try:
            memo_type = memo.get("type", "once")
            schedule = memo.get("schedule", "")
            title = memo.get("title", "")
            task = memo.get("task", "")

            if memo_type == "cron":
                memo_id = await self._task_scheduler.add_memo(
                    user_id=user_id,
                    platform="whatsapp",
                    schedule_type="cron",
                    prompt=task,
                    description=title,
                    cron=schedule,
                )
                logger.info(
                    f"Created recurring memo {memo_id} for user {user_id}: {title} ({schedule})"
                )
                return memo_id
            else:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                run_at = datetime.fromisoformat(schedule)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=ZoneInfo("Europe/Rome"))

                memo_id = await self._task_scheduler.add_memo(
                    user_id=user_id,
                    platform="whatsapp",
                    schedule_type="once",
                    prompt=task,
                    description=title,
                    run_at=run_at,
                )
                logger.info(
                    f"Created one-time memo {memo_id} for user {user_id}: {title} ({run_at})"
                )
                return memo_id

        except Exception as e:
            logger.error(f"Failed to create memo from tag: {e}")

    @staticmethod
    def _extract_file_paths(text: str) -> List[str]:
        """Extract file paths from [SEND_FILE: path] tags."""
        matches = re.findall(r"\[SEND_FILE:\s*(.+?)\]", text, re.IGNORECASE)
        if matches:
            logger.warning(
                "DEPRECATED: [SEND_FILE:] tag used — migrate to send_file_to_chat MCP tool"
            )
        resolved = []
        for m in matches:
            path = m.strip()
            p = Path(path)
            if not p.is_absolute() and not p.exists():
                for search_dir in ["/app/data/playwright", "/app/data"]:
                    candidate = Path(search_dir) / p
                    if candidate.exists():
                        path = str(candidate)
                        logger.info(f"Resolved relative path '{m.strip()}' → '{path}'")
                        break
            resolved.append(path)
        return resolved

    @staticmethod
    def _remove_file_tags(text: str) -> str:
        """Remove [SEND_FILE: ...] tags from text."""
        return re.sub(r"\[SEND_FILE:\s*.+?\]", "", text, flags=re.IGNORECASE).strip()

    # --- Rate Limiting (Phase 5) ---

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits. Returns True if OK to send."""
        now = time.time()
        cutoff = now - RATE_LIMIT_WINDOW
        self._message_timestamps = [t for t in self._message_timestamps if t > cutoff]
        if len(self._message_timestamps) >= RATE_LIMIT_MAX:
            return False
        self._message_timestamps.append(now)
        return True

    # --- Message Dedup (Phase 5) ---

    def _is_duplicate(self, message_id: str) -> bool:
        """Check if message was already seen. Returns True if duplicate."""
        if not message_id:
            return False
        now = time.time()
        if message_id in self._seen_messages:
            return True
        self._seen_messages[message_id] = now
        return False

    async def _dedup_cleanup_loop(self) -> None:
        """Periodically clean up old dedup entries."""
        try:
            while True:
                await asyncio.sleep(DEDUP_TTL)
                now = time.time()
                cutoff = now - DEDUP_TTL
                expired = [k for k, v in self._seen_messages.items() if v < cutoff]
                for k in expired:
                    del self._seen_messages[k]
                if expired:
                    logger.debug(
                        f"WhatsApp dedup cleanup: removed {len(expired)} entries"
                    )
        except asyncio.CancelledError:
            pass

    # --- Health Check (Phase 5) ---

    async def health_check(self) -> dict:
        """Check WhatsApp connection health."""
        if not self._client:
            return {"status": "error", "message": "Client not initialized"}
        try:
            result = await self._client.get_connection_status()
            state = (
                result.get("instance", {}).get("state", "unknown")
                if result
                else "unknown"
            )
            info = {"status": "ok" if state == "open" else "degraded", "state": state}
            if self._user_clients:
                info["user_instances"] = len(self._user_clients)
            return info
        except Exception as e:
            return {"status": "error", "message": str(e)}
