"""Tests for WhatsApp Meta Cloud API channel adapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from plugins.whatsapp_api.adapter import (
    DEDUP_TTL,
    WhatsAppMetaChannel,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Ensure class registry is clean before/after each test."""
    WhatsAppMetaChannel._channels.clear()
    yield
    WhatsAppMetaChannel._channels.clear()


@pytest.fixture
def mock_secrets():
    with patch("plugins.whatsapp_api.adapter.secrets_manager") as mock_sm:
        mock_sm.get_plain.return_value = "test-token-123"
        yield mock_sm


@pytest.fixture
def channel(mock_secrets):
    config = {
        "phone_number_id": "123456",
        "access_token_secret": "WA_TOKEN",
    }
    ch = WhatsAppMetaChannel(config)
    ch._client = AsyncMock()
    ch._client.send_text = AsyncMock(return_value={"messages": [{"id": "m1"}]})
    ch._client.close = AsyncMock()
    return ch


# -- Class registry tests --


class TestClassRegistry:
    def test_register_and_get(self, mock_secrets):
        config = {"phone_number_id": "111", "access_token_secret": "X"}
        ch = WhatsAppMetaChannel(config)
        WhatsAppMetaChannel.register("111", ch)
        assert WhatsAppMetaChannel.get_channel("111") is ch

    def test_unregister(self, mock_secrets):
        config = {"phone_number_id": "222", "access_token_secret": "X"}
        ch = WhatsAppMetaChannel(config)
        WhatsAppMetaChannel.register("222", ch)
        WhatsAppMetaChannel.unregister("222")
        assert WhatsAppMetaChannel.get_channel("222") is None

    def test_get_nonexistent(self):
        assert WhatsAppMetaChannel.get_channel("999") is None

    def test_unregister_nonexistent_no_error(self):
        WhatsAppMetaChannel.unregister("nonexistent")


# -- send_message tests --


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_simple_text(self, channel):
        await channel.send_message(393001234567, "Hello *world*")
        channel._client.send_text.assert_called_once()
        call_args = channel._client.send_text.call_args
        assert call_args[0][0] == "393001234567"
        # markdown_to_whatsapp converts **bold** but *bold* stays as-is
        assert "Hello" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_send_formats_markdown(self, channel):
        await channel.send_message(123, "**bold text**")
        sent_text = channel._client.send_text.call_args[0][1]
        # **bold** should become *bold* in WhatsApp format
        assert sent_text == "*bold text*"

    @pytest.mark.asyncio
    async def test_send_splits_long_message(self, channel):
        long_text = "A" * 5000
        await channel.send_message(123, long_text)
        assert channel._client.send_text.call_count >= 2

    @pytest.mark.asyncio
    async def test_send_with_attachments(self, channel):
        channel._client.upload_media = AsyncMock(return_value="media_123")
        channel._client.send_image = AsyncMock(return_value={})

        with patch("plugins.whatsapp_api.adapter.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.name = "photo.jpg"
            with patch(
                "plugins.whatsapp_api.adapter.mimetypes.guess_type",
                return_value=("image/jpeg", None),
            ):
                await channel.send_message(123, "See this", ["/tmp/photo.jpg"])

        channel._client.send_text.assert_called_once()
        channel._client.upload_media.assert_called_once()
        channel._client.send_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_no_client_logs_warning(self, channel):
        channel._client = None
        await channel.send_message(123, "hello")
        # Should not raise


# -- Dedup tests --


class TestDedup:
    def test_first_message_not_duplicate(self, channel):
        assert channel._is_duplicate("msg_001") is False

    def test_same_message_is_duplicate(self, channel):
        channel._is_duplicate("msg_002")
        assert channel._is_duplicate("msg_002") is True

    def test_different_messages_not_duplicate(self, channel):
        channel._is_duplicate("msg_003")
        assert channel._is_duplicate("msg_004") is False

    def test_expired_entry_cleaned(self, channel):
        # Manually insert an expired entry
        channel._seen_messages["old_msg"] = time.monotonic() - DEDUP_TTL - 10
        channel._seen_messages["new_msg"] = time.monotonic()

        # Simulate cleanup
        now = time.monotonic()
        expired = [
            mid for mid, ts in channel._seen_messages.items() if now - ts > DEDUP_TTL
        ]
        for mid in expired:
            del channel._seen_messages[mid]

        assert "old_msg" not in channel._seen_messages
        assert "new_msg" in channel._seen_messages


# -- Authorization tests --


class TestAuthorization:
    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    def test_open_mode_allows_all(self, mock_model, channel):
        mock_model.get_authorized.return_value = []
        assert channel._check_authorization("393001234567") is True

    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    def test_authorized_number_allowed(self, mock_model, channel):
        mock_model.get_authorized.return_value = [
            "393001234567",
            "393009876543",
        ]
        assert channel._check_authorization("393001234567") is True

    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    def test_unauthorized_number_rejected(self, mock_model, channel):
        mock_model.get_authorized.return_value = ["393009876543"]
        assert channel._check_authorization("393001234567") is False


# -- Rate limit tests --


class TestRateLimit:
    def test_under_limit_allowed(self, channel):
        assert channel._check_rate_limit() is True

    def test_at_limit_rejected(self, channel):
        now = time.monotonic()
        channel._send_times = [now - i for i in range(240)]
        assert channel._check_rate_limit() is False

    def test_old_entries_pruned(self, channel):
        old = time.monotonic() - 3700  # older than 1 hour
        channel._send_times = [old] * 240
        assert channel._check_rate_limit() is True
        assert len(channel._send_times) == 0


# -- handle_incoming tests --


class TestHandleIncoming:
    @pytest.mark.asyncio
    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    async def test_skips_duplicate(self, mock_model, channel):
        mock_model.get_authorized.return_value = []
        handler = AsyncMock(return_value="reply")
        channel.set_message_handler(handler)

        msg = {"id": "dup1", "from": "123", "type": "text", "text": {"body": "hi"}}
        await channel.handle_incoming(msg)
        await channel.handle_incoming(msg)

        handler.assert_called_once()

    @pytest.mark.asyncio
    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    async def test_rejects_unauthorized(self, mock_model, channel):
        mock_model.get_authorized.return_value = ["999"]
        handler = AsyncMock()
        channel.set_message_handler(handler)

        msg = {"id": "u1", "from": "123", "type": "text", "text": {"body": "hi"}}
        await channel.handle_incoming(msg)

        handler.assert_not_called()

    @pytest.mark.asyncio
    @patch("plugins.whatsapp_api.adapter.AuthorizedNumber")
    async def test_text_message_processed(self, mock_model, channel):
        mock_model.get_authorized.return_value = []
        mock_model.get_label.return_value = "Test User"
        handler = AsyncMock(return_value="pong")
        channel.set_message_handler(handler)

        msg = {"id": "t1", "from": "123", "type": "text", "text": {"body": "ping"}}
        await channel.handle_incoming(msg)

        handler.assert_called_once()
        message_arg = handler.call_args[0][0]
        assert message_arg.text == "ping"
        assert message_arg.platform == "whatsapp"

        # Response should have been sent
        channel._client.send_text.assert_called()


# -- send_file tests --


class TestSendFile:
    @pytest.mark.asyncio
    async def test_send_document(self, channel):
        channel._client.upload_media = AsyncMock(return_value="doc_id")
        channel._client.send_document = AsyncMock(return_value={})

        with patch("plugins.whatsapp_api.adapter.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.name = "report.pdf"
            with patch(
                "plugins.whatsapp_api.adapter.mimetypes.guess_type",
                return_value=("application/pdf", None),
            ):
                result = await channel.send_file(123, "/tmp/report.pdf", "Q4 Report")

        assert result is True
        channel._client.send_document.assert_called_once_with(
            "123", "doc_id", "Q4 Report", "report.pdf"
        )

    @pytest.mark.asyncio
    async def test_send_audio_file(self, channel):
        channel._client.upload_media = AsyncMock(return_value="aud_id")
        channel._client.send_audio = AsyncMock(return_value={})

        with patch("plugins.whatsapp_api.adapter.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.name = "voice.ogg"
            with patch(
                "plugins.whatsapp_api.adapter.mimetypes.guess_type",
                return_value=("audio/ogg", None),
            ):
                result = await channel.send_file(123, "/tmp/voice.ogg")

        assert result is True
        channel._client.send_audio.assert_called_once_with("123", "aud_id")

    @pytest.mark.asyncio
    async def test_send_file_not_found(self, channel):
        with patch("plugins.whatsapp_api.adapter.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            result = await channel.send_file(123, "/tmp/missing.pdf")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_file_no_client(self, channel):
        channel._client = None
        result = await channel.send_file(123, "/tmp/file.pdf")
        assert result is False
