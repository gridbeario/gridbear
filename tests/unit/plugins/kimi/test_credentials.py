"""Kimi credential resolution: the API-key-only surface.

The design's original test file for this module also covered a
four-outcome OAuth refresh accessor, two digests, and a degraded mark. None
of that ships this iteration — the CLI backend it depended on was dropped,
and the Kimi Code subscription's credential is held by the CLI itself, not
by us (see plugins/kimi/credentials.py's module docstring). Only the
API-key path this module actually implements is tested here.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def vault(monkeypatch):
    """An in-memory stand-in for the secrets manager."""
    store: dict[str, str] = {}

    class _Vault:
        def get_plain(self, key):
            return store.get(key)

        def set(self, key, value):
            store[key] = value

        def delete(self, key):
            store.pop(key, None)

    fake = _Vault()
    monkeypatch.setattr("ui.secrets_manager.secrets_manager", fake)
    return store


class TestReadApiKey:
    def test_returns_configured_key(self, vault):
        from plugins.kimi.credentials import API_KEY_VAULT_KEY, read_api_key

        vault[API_KEY_VAULT_KEY] = "sk-abc123"

        assert read_api_key() == "sk-abc123"

    def test_returns_empty_string_when_absent(self, vault):
        from plugins.kimi.credentials import read_api_key

        assert read_api_key() == ""

    def test_whitespace_only_key_treated_as_absent(self, vault):
        """Decision, made in this task: a whitespace-only vault value is
        not a usable key. It cannot be a valid sk-... key, so treating it
        as present would send a bearer token guaranteed to 401 instead of
        surfacing the "not configured" message. Testing the "treated as
        absent" side of that decision, deliberately.
        """
        from plugins.kimi.credentials import API_KEY_VAULT_KEY, read_api_key

        vault[API_KEY_VAULT_KEY] = "   "

        assert read_api_key() == ""


class TestResolveForTurn:
    def test_configured_key_is_returned(self, vault):
        from plugins.kimi.credentials import (
            API_KEY_VAULT_KEY,
            CredentialKind,
            resolve_for_turn,
        )

        vault[API_KEY_VAULT_KEY] = "sk-abc123"
        resolved = resolve_for_turn()

        assert resolved.kind is CredentialKind.API_KEY
        assert resolved.secret == "sk-abc123"
        assert resolved.message is None

    def test_no_key_configured_produces_no_credential_message(self, vault):
        from plugins.kimi.credentials import MSG_NO_CREDENTIAL, resolve_for_turn

        resolved = resolve_for_turn()

        assert resolved.secret == ""
        assert resolved.message == MSG_NO_CREDENTIAL

    def test_whitespace_only_key_resolves_as_no_credential(self, vault):
        """Consistent with TestReadApiKey: a whitespace-only stored value
        must not slip through resolve_for_turn as a usable secret either.
        """
        from plugins.kimi.credentials import (
            API_KEY_VAULT_KEY,
            MSG_NO_CREDENTIAL,
            resolve_for_turn,
        )

        vault[API_KEY_VAULT_KEY] = "   "
        resolved = resolve_for_turn()

        assert resolved.secret == ""
        assert resolved.message == MSG_NO_CREDENTIAL


class TestVaultKeyCoupling:
    def test_matches_manifest_env_name(self):
        """The coupling that breaks silently if either side is renamed:
        ui/routes/plugins.py writes the key entered on /plugins/kimi under
        the manifest's config_schema.moonshot_api_key.env name, and this
        module reads it back under API_KEY_VAULT_KEY. They must match.
        """
        from plugins.kimi.credentials import API_KEY_VAULT_KEY

        manifest_path = (
            Path(__file__).resolve().parents[4] / "plugins" / "kimi" / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text())
        env_name = manifest["config_schema"]["moonshot_api_key"]["env"]

        assert API_KEY_VAULT_KEY == "MOONSHOT_API_KEY"
        assert API_KEY_VAULT_KEY == env_name


class TestMessages:
    def test_messages_are_distinct_and_non_empty(self):
        from plugins.kimi import credentials

        assert credentials.MSG_NO_CREDENTIAL
        assert credentials.MSG_OAUTH_UNSUPPORTED
        assert credentials.MSG_NO_CREDENTIAL != credentials.MSG_OAUTH_UNSUPPORTED

    def test_messages_name_the_action_that_resolves_them(self):
        from plugins.kimi import credentials

        # Both point at the same recovery action available in this build:
        # setting the API key. MSG_NO_CREDENTIAL also names the OAuth
        # connect action, unreachable in this build but still surfaced by
        # the message (see plugins/kimi/credentials.py module docstring).
        assert "MOONSHOT_API_KEY" in credentials.MSG_NO_CREDENTIAL
        assert "MOONSHOT_API_KEY" in credentials.MSG_OAUTH_UNSUPPORTED
