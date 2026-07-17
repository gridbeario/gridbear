"""Tests for OAuth2 dynamic client registration — client_credentials flow.

Verifies:
1. Confidential client without redirect_uris is accepted (client_credentials)
2. Public client without redirect_uris is rejected
3. agent_name is passed through to db.create_client()
4. grant_types includes client_credentials when no redirect_uris
5. Backward compatibility: redirect_uris flow still works
"""

import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Stub optional dependencies only when genuinely absent. Checking `not in
# sys.modules` is not enough: an installed-but-not-yet-imported package would be
# stubbed with a MagicMock that then leaks into later tests (e.g. `from
# webauthn.helpers import ...` failing with "'webauthn' is not a package").
for _mod in ("pyotp", "qrcode", "webauthn"):
    try:
        importlib.import_module(_mod)
    except ImportError:
        sys.modules[_mod] = MagicMock()

from core.oauth2.server import register_client


def _make_request(body: dict):
    """Build a fake Starlette Request with the given JSON body."""
    request = AsyncMock()
    request.json = AsyncMock(return_value=body)
    request.base_url = "http://localhost:8088/"
    return request


def _make_mock_client(client_id="test-id", client_type="confidential", name="test"):
    client = MagicMock()
    client.client_id = client_id
    client.client_type = client_type
    client.name = name
    client.get_redirect_uris.return_value = []
    return client


class TestClientCredentialsRegistration:
    """Test registering confidential clients without redirect_uris."""

    @pytest.mark.asyncio
    async def test_confidential_no_redirect_uris_succeeds(self):
        """Confidential client without redirect_uris should succeed."""
        mock_client = _make_mock_client()
        mock_db = MagicMock()
        mock_db.create_client.return_value = (mock_client, "secret123")

        request = _make_request(
            {
                "client_name": "gridbear-cli",
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "mcp api",
                "agent_name": "cli",
            }
        )

        with (
            patch("core.oauth2.server.get_db", return_value=mock_db),
            patch.dict("os.environ", {"GRIDBEAR_BASE_URL": "http://localhost:8088"}),
        ):
            response = await register_client(request)

        assert response.status_code == 201
        import json

        data = json.loads(response.body)
        assert data["client_id"] == "test-id"
        assert data["client_secret"] == "secret123"
        assert "client_credentials" in data["grant_types"]
        assert "authorization_code" not in data["grant_types"]

    @pytest.mark.asyncio
    async def test_agent_name_passed_to_create_client(self):
        """agent_name from request body should be passed to db.create_client."""
        mock_client = _make_mock_client()
        mock_db = MagicMock()
        mock_db.create_client.return_value = (mock_client, "secret123")

        request = _make_request(
            {
                "client_name": "gridbear-cli",
                "token_endpoint_auth_method": "client_secret_post",
                "agent_name": "cli-dev",
            }
        )

        with (
            patch("core.oauth2.server.get_db", return_value=mock_db),
            patch.dict("os.environ", {"GRIDBEAR_BASE_URL": "http://localhost:8088"}),
        ):
            await register_client(request)

        call_kwargs = mock_db.create_client.call_args.kwargs
        assert call_kwargs["agent_name"] == "cli-dev"

    @pytest.mark.asyncio
    async def test_confidential_no_redirect_sets_active_and_no_pkce(self):
        """client_credentials client: active=True, require_pkce=False."""
        mock_client = _make_mock_client()
        mock_db = MagicMock()
        mock_db.create_client.return_value = (mock_client, "secret123")

        request = _make_request(
            {
                "client_name": "gridbear-cli",
                "token_endpoint_auth_method": "client_secret_post",
            }
        )

        with (
            patch("core.oauth2.server.get_db", return_value=mock_db),
            patch.dict("os.environ", {"GRIDBEAR_BASE_URL": "http://localhost:8088"}),
        ):
            await register_client(request)

        call_kwargs = mock_db.create_client.call_args.kwargs
        assert call_kwargs["active"] is True
        assert call_kwargs["require_pkce"] is False

    @pytest.mark.asyncio
    async def test_public_no_redirect_uris_rejected(self):
        """Public client (auth_method=none) without redirect_uris should fail."""
        request = _make_request(
            {
                "client_name": "public-app",
                "token_endpoint_auth_method": "none",
                # No redirect_uris
            }
        )

        response = await register_client(request)
        assert response.status_code == 400


class TestRedirectUriFlowUnchanged:
    """Verify backward compatibility: redirect_uris registration still works."""

    @pytest.mark.asyncio
    async def test_redirect_uris_flow_sets_pkce_and_auth_code(self):
        """When redirect_uris are provided, require_pkce=True and grant_types=auth_code."""
        mock_client = _make_mock_client(client_type="public")
        mock_client.get_redirect_uris.return_value = ["https://claude.ai/callback"]
        mock_db = MagicMock()
        mock_db.create_client.return_value = (mock_client, None)

        request = _make_request(
            {
                "client_name": "web-app",
                "redirect_uris": ["https://claude.ai/callback"],
                "token_endpoint_auth_method": "none",
            }
        )

        with (
            patch("core.oauth2.server.get_db", return_value=mock_db),
            patch(
                "core.oauth2.config.get_gateway_config",
                return_value={"trusted_domains": ["claude.ai"]},
            ),
            patch.dict("os.environ", {"GRIDBEAR_BASE_URL": "http://localhost:8088"}),
        ):
            response = await register_client(request)

        assert response.status_code == 201
        import json

        data = json.loads(response.body)
        assert "authorization_code" in data["grant_types"]

        call_kwargs = mock_db.create_client.call_args.kwargs
        assert call_kwargs["require_pkce"] is True
