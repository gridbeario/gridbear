from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.whatsapp_api.meta_client import (
    MetaApiError,
    MetaAuthError,
    MetaClient,
    MetaRateLimitError,
)


@pytest.fixture
def client():
    return MetaClient(phone_number_id="123456", access_token="test-token")


def _mock_response(status=200, json_data=None, text="", headers=None):
    """Create a mock aiohttp response as an async context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    resp.headers = headers or {"Content-Type": "application/json"}
    resp.read = AsyncMock(return_value=b"")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
async def test_send_text_correct_payload(client):
    """send_text calls the API with the correct payload."""
    mock_session = MagicMock()
    success_resp = _mock_response(200, json_data={"messages": [{"id": "wamid.abc123"}]})
    mock_session.request = MagicMock(return_value=success_resp)
    client._session = mock_session

    result = await client.send_text("+1234567890", "Hello!")

    mock_session.request.assert_called_once()
    call_args = mock_session.request.call_args
    assert call_args[0][0] == "POST"
    assert "123456/messages" in call_args[0][1]

    payload = call_args[1]["json"]
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "+1234567890"
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "Hello!"
    assert result == {"messages": [{"id": "wamid.abc123"}]}


@pytest.mark.asyncio
async def test_retry_on_5xx(client):
    """First call returns 500, second returns 200 -- should succeed."""
    mock_session = MagicMock()

    error_resp = _mock_response(500, text="Internal Server Error")
    success_resp = _mock_response(200, json_data={"messages": [{"id": "wamid.ok"}]})
    mock_session.request = MagicMock(side_effect=[error_resp, success_resp])
    client._session = mock_session

    with patch(
        "plugins.whatsapp_api.meta_client.asyncio.sleep", new_callable=AsyncMock
    ):
        result = await client.send_text("+1234567890", "Retry test")

    assert result == {"messages": [{"id": "wamid.ok"}]}
    assert mock_session.request.call_count == 2


@pytest.mark.asyncio
async def test_auth_error_on_401(client):
    """401 response raises MetaAuthError immediately (no retry)."""
    mock_session = MagicMock()
    error_resp = _mock_response(401, text="Unauthorized")
    mock_session.request = MagicMock(return_value=error_resp)
    client._session = mock_session

    with pytest.raises(MetaAuthError) as exc_info:
        await client.send_text("+1234567890", "Auth test")

    assert exc_info.value.status == 401
    # No retry on 401
    assert mock_session.request.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_error_after_retries(client):
    """429 after all retries raises MetaRateLimitError."""
    mock_session = MagicMock()
    rate_resp = _mock_response(429, text="Rate limited")
    mock_session.request = MagicMock(return_value=rate_resp)
    client._session = mock_session

    with patch(
        "plugins.whatsapp_api.meta_client.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(MetaRateLimitError) as exc_info:
            await client.send_text("+1234567890", "Rate test")

    assert exc_info.value.status == 429
    assert mock_session.request.call_count == 3


@pytest.mark.asyncio
async def test_client_error_on_400(client):
    """400 response raises MetaApiError immediately (no retry)."""
    mock_session = MagicMock()
    error_resp = _mock_response(400, text="Bad Request")
    mock_session.request = MagicMock(return_value=error_resp)
    client._session = mock_session

    with pytest.raises(MetaApiError) as exc_info:
        await client.send_text("+1234567890", "Bad request test")

    assert exc_info.value.status == 400
    assert mock_session.request.call_count == 1


@pytest.mark.asyncio
async def test_send_image_with_caption(client):
    """send_image includes caption in payload."""
    mock_session = MagicMock()
    success_resp = _mock_response(200, json_data={"messages": [{"id": "wamid.img"}]})
    mock_session.request = MagicMock(return_value=success_resp)
    client._session = mock_session

    await client.send_image("+1234567890", "media-123", caption="A photo")

    payload = mock_session.request.call_args[1]["json"]
    assert payload["type"] == "image"
    assert payload["image"]["id"] == "media-123"
    assert payload["image"]["caption"] == "A photo"


@pytest.mark.asyncio
async def test_mark_read(client):
    """mark_read sends correct payload and returns None."""
    mock_session = MagicMock()
    success_resp = _mock_response(200, json_data={"success": True})
    mock_session.request = MagicMock(return_value=success_resp)
    client._session = mock_session

    result = await client.mark_read("wamid.xyz")

    assert result is None
    payload = mock_session.request.call_args[1]["json"]
    assert payload["status"] == "read"
    assert payload["message_id"] == "wamid.xyz"


@pytest.mark.asyncio
async def test_no_session_raises_runtime_error(client):
    """Calling _request without start() raises RuntimeError."""
    with pytest.raises(RuntimeError, match="session not started"):
        await client.send_text("+1234567890", "No session")
