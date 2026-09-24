"""The refresh route's two rules, which no existing runner's copy satisfies."""

import pytest

import core.registry  # noqa: F401 — force module import before monkeypatch
from core.models_registry import ModelsRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = ModelsRegistry(base_dir=tmp_path)
    monkeypatch.setattr("core.registry.get_models_registry", lambda: reg)
    return reg


class TestApiIdResolution:
    def test_a_hand_curated_mapping_survives_a_refresh(self, registry):
        # set_models() overwrites the file whole, and the four existing
        # refresh implementations build entries without api_id at all.
        from plugins.kimi.api.routes import _resolve_api_ids

        registry.set_models(
            "kimi", [{"id": "turbo", "name": "Turbo", "api_id": "billed-turbo"}]
        )
        resolved = _resolve_api_ids([{"id": "turbo", "name": "Turbo (new name)"}])
        assert resolved == [
            {"id": "turbo", "name": "Turbo (new name)", "api_id": "billed-turbo"}
        ]

    def test_an_unseen_id_defaults_its_api_id_to_itself(self, registry):
        from plugins.kimi.api.routes import _resolve_api_ids

        registry.set_models("kimi", [])
        resolved = _resolve_api_ids([{"id": "brand-new", "name": "New"}])
        assert resolved[0]["api_id"] == "brand-new"

    def test_every_entry_carries_an_api_id_after_a_refresh(self, registry):
        from plugins.kimi.api.routes import _resolve_api_ids

        registry.set_models("kimi", [{"id": "a", "name": "A", "api_id": "billed-a"}])
        resolved = _resolve_api_ids(
            [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        )
        assert all("api_id" in entry for entry in resolved)

    def test_no_model_previously_priced_drops_to_zero(self, registry):
        from plugins.kimi.api.routes import _resolve_api_ids
        from plugins.kimi.cost_tracker import KIMI_PRICING, calculate_cost

        assert KIMI_PRICING, "the price table is empty — row 9 was not transcribed"
        priced_prefix = KIMI_PRICING[0][0]
        registry.set_models(
            "kimi", [{"id": "ui-name", "name": "X", "api_id": priced_prefix}]
        )
        resolved = _resolve_api_ids([{"id": "ui-name", "name": "X"}])
        assert calculate_cost(resolved[0]["api_id"], 1_000_000, 0) > 0


class TestThePlaceholderRule:
    def test_the_refresh_never_marks_an_entry_as_a_placeholder(self, registry):
        # The direction a copy gets wrong in reverse. Those ids come from
        # Moonshot and are real; marking them would hand the boot check a
        # startup failure on correct data, from a correct operator action.
        from plugins.kimi.api.routes import _resolve_api_ids

        registry.set_models(
            "kimi",
            [
                {
                    "id": "seeded",
                    "name": "Seeded",
                    "api_id": "seeded",
                    "placeholder": True,
                }
            ],
        )
        resolved = _resolve_api_ids([{"id": "seeded", "name": "Seeded (real)"}])
        assert "placeholder" not in resolved[0]

    def test_refreshing_over_a_marked_registry_clears_the_marks(self, registry):
        from plugins.kimi.api.routes import _resolve_api_ids

        registry.set_models(
            "kimi",
            [
                {"id": "a", "name": "A", "api_id": "a", "placeholder": True},
                {"id": "b", "name": "B", "api_id": "b", "placeholder": True},
            ],
        )
        resolved = _resolve_api_ids(
            [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
        )
        assert not any(entry.get("placeholder") for entry in resolved)


class TestTheEightEndpointsExist:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/models"),
            ("POST", "/models"),
            ("POST", "/models/refresh"),
            ("GET", "/auth/status"),
            ("POST", "/auth/logout"),
            ("POST", "/auth/login"),
            ("POST", "/auth/code"),
            ("POST", "/auth/api-key"),
            ("GET", "/health"),
        ],
    )
    def test_the_route_is_declared(self, method, path):
        # The generic runner page addresses all of these by plugin name and
        # the boot check names a file only the models page can repair, so a
        # missing route is a runner that cannot be fixed from the UI.
        from plugins.kimi.api.routes import router

        declared = {
            (verb, route.path)
            for route in router.routes
            for verb in getattr(route, "methods", set())
        }
        assert (method, path) in declared

    def test_the_token_endpoint_matches_the_manifest(self):
        import json
        from pathlib import Path

        from plugins.kimi.api.routes import router

        manifest = json.loads((Path("plugins/kimi/manifest.json")).read_text())
        token_endpoint = manifest["cli_meta"]["token_endpoint"]
        declared = {route.path for route in router.routes}
        assert f"/auth/{token_endpoint}" in declared, (
            f"the manifest promises /auth/{token_endpoint} and the page calls "
            f"it at config.html:158, but the router does not declare it"
        )


class TestAuthStatusShape:
    """auth_status reports what credentials.py can actually produce.

    No is_degraded/read_oauth_block/OAUTH_SUPPORTED: credentials.py ships
    only an API key this iteration (see its module docstring), so the
    status route has nothing OAuth-shaped to report.
    """

    def test_reports_logged_in_false_with_no_api_key(self, registry, monkeypatch):
        import asyncio

        from plugins.kimi.api import routes

        monkeypatch.setattr(routes, "read_api_key", lambda: "")
        monkeypatch.setattr(routes, "get_auth_error_info", lambda: None)

        result = asyncio.run(routes.auth_status())
        assert result.data["loggedIn"] is False

    def test_reports_logged_in_true_with_an_api_key(self, registry, monkeypatch):
        import asyncio

        from plugins.kimi.api import routes

        monkeypatch.setattr(routes, "read_api_key", lambda: "sk-abc")
        monkeypatch.setattr(routes, "get_auth_error_info", lambda: None)

        result = asyncio.run(routes.auth_status())
        assert result.data["loggedIn"] is True

    def test_surfaces_a_recent_auth_error(self, registry, monkeypatch):
        import asyncio

        from plugins.kimi.api import routes

        monkeypatch.setattr(routes, "read_api_key", lambda: "sk-abc")
        monkeypatch.setattr(routes, "get_auth_error_info", lambda: {"timestamp": 123.0})

        result = asyncio.run(routes.auth_status())
        assert result.data["authError"] == {"timestamp": 123.0}
        assert result.data["token_expired"] is True


class TestOAuthRoutesFailClosed:
    """No OAuth flow ships this iteration; the buttons must not 404."""

    def test_login_names_the_api_key_path_not_a_missing_cli(self):
        import asyncio

        from plugins.kimi.api.routes import auth_login

        response = asyncio.run(auth_login())
        assert response.status_code == 400
        body = response.body.decode()
        assert "api-key" in body
        # The bug this plugin already removed once: blaming a CLI that
        # does not ship here for lacking a feature it was never asked for.
        assert "CLI" not in body

    def test_code_exchange_is_also_unsupported(self):
        import asyncio

        from plugins.kimi.api.routes import AuthCodeRequest, auth_code

        response = asyncio.run(auth_code(AuthCodeRequest(code="anything")))
        assert response.status_code == 400


class TestLogoutDeletesOnlyTheApiKey:
    def test_logout_deletes_the_vault_key(self, monkeypatch):
        import asyncio

        from plugins.kimi.api import routes

        deleted = []
        monkeypatch.setattr(
            routes.secrets_manager, "delete", lambda key: deleted.append(key)
        )

        asyncio.run(routes.auth_logout())
        assert deleted == [routes.API_KEY_VAULT_KEY]


class TestBaseUrlResolution:
    """_base_url() must agree with the order every other Moonshot caller in
    this plugin uses (KimiRunner.__init__, KimiApiBackend.__init__): plugin
    config first, then KIMI_BASE_URL, then the literal default. Before this
    fix this route read only the env var — an operator's base_url override
    on the plugin config page (e.g. switching to api.moonshot.cn) reached
    turns but not /health or /models/refresh, so a working runner showed
    "API returned 401" here.
    """

    def test_a_configured_base_url_wins_over_env_and_default(self, monkeypatch):
        import ui.plugin_helpers as plugin_helpers
        from plugins.kimi.api.routes import _base_url

        monkeypatch.setattr(
            plugin_helpers,
            "load_plugin_config",
            lambda name: {"base_url": "https://api.moonshot.cn/v1"},
        )
        monkeypatch.setenv("KIMI_BASE_URL", "https://env-only.example/v1")

        assert _base_url() == "https://api.moonshot.cn/v1"

    def test_env_wins_when_no_config_override_is_set(self, monkeypatch):
        import ui.plugin_helpers as plugin_helpers
        from plugins.kimi.api.routes import _base_url

        monkeypatch.setattr(plugin_helpers, "load_plugin_config", lambda name: {})
        monkeypatch.setenv("KIMI_BASE_URL", "https://env-only.example/v1")

        assert _base_url() == "https://env-only.example/v1"

    def test_the_literal_default_when_nothing_is_configured(self, monkeypatch):
        import ui.plugin_helpers as plugin_helpers
        from plugins.kimi.api.routes import _DEFAULT_BASE_URL, _base_url

        monkeypatch.setattr(plugin_helpers, "load_plugin_config", lambda name: {})
        monkeypatch.delenv("KIMI_BASE_URL", raising=False)

        assert _base_url() == _DEFAULT_BASE_URL
