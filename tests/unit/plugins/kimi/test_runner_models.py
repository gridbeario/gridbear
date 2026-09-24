"""Model identity for the Kimi runner.

available_models is a property and both of its branches must return the same
type: if the fallback returned dicts, the admin dropdown it exists to protect
would break on exactly the fresh install it covers.
"""

import pytest

import core.registry  # noqa: F401 — force module import before monkeypatch
from core.models_registry import ModelsRegistry


@pytest.fixture
def registry(tmp_path):
    return ModelsRegistry(base_dir=tmp_path)


@pytest.fixture
def runner(registry, monkeypatch):
    monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)
    from plugins.kimi.runner import KimiRunner

    return KimiRunner({})


class TestAvailableModels:
    def test_it_is_a_property_not_a_method(self, runner):
        # Read without parentheses, as tests/unit/test_model_api_routes.py:31
        # and every caller in core/ do.
        models = runner.available_models
        assert isinstance(models, list)

    def test_the_fallback_returns_two_tuples_like_the_registry_branch(self, runner):
        # Registry is empty here, so this exercises the fallback.
        models = runner.available_models
        assert models, "fallback must not be empty — it protects the dropdown"
        for entry in models:
            assert isinstance(entry, tuple) and len(entry) == 2
            assert all(isinstance(part, str) for part in entry)

    def test_neither_api_id_nor_placeholder_leaves_through_the_property(self, runner):
        from plugins.kimi.runner import KimiRunner

        projected = set()
        for pair in runner.available_models:
            projected.update(pair)
        for model in KimiRunner._DEFAULT_MODELS:
            if model["api_id"] != model["id"]:
                assert model["api_id"] not in projected

    def test_the_registry_branch_wins_when_the_registry_has_entries(
        self, runner, registry
    ):
        registry.set_models(
            "kimi", [{"id": "ui-x", "name": "Model X", "api_id": "billed-x"}]
        )
        assert runner.available_models == [("ui-x", "Model X")]


class TestSeeding:
    @pytest.mark.asyncio
    async def test_initialize_seeds_the_registry_on_a_fresh_install(
        self, runner, registry
    ):
        from plugins.kimi.runner import KimiRunner

        assert registry.get_models("kimi") == []
        await runner.initialize()
        seeded = registry.get_models("kimi")
        assert len(seeded) == len(KimiRunner._DEFAULT_MODELS)
        # The real catalogue, captured from GET /v1/models — not the
        # provisional ids this constant shipped with.
        assert {m["id"] for m in seeded} == {"kimi-k2.6", "kimi-k2.7-code"}

    async def test_no_shipped_model_is_still_a_placeholder(self, runner):
        # The marker is the only thing separating a filled-in table from an
        # invented one: invented ids and an invented price list agree with
        # each other. Its absence is what lets initialize() start.
        from plugins.kimi.runner import KimiRunner

        marked = [m["id"] for m in KimiRunner._DEFAULT_MODELS if m.get("placeholder")]
        assert marked == []

    async def test_every_shipped_model_declares_both_identifier_spaces(self, runner):
        from plugins.kimi.runner import KimiRunner

        for model in KimiRunner._DEFAULT_MODELS:
            assert set(model) >= {"id", "name", "api_id"}

    async def test_initialize_refuses_to_start_on_placeholder_ids(
        self, runner, registry
    ):
        # seed_if_empty does not overwrite an existing file
        # (core/models_registry.py:99-102), so a machine booted once on
        # placeholders keeps serving them until the file is rewritten.
        registry.set_models(
            "kimi",
            [{"id": "guess", "name": "Guess", "api_id": "guess", "placeholder": True}],
        )
        with pytest.raises(RuntimeError, match="data/models/kimi.json"):
            await runner.initialize()


class TestAuthErrorRecognition:
    # The constants now carry row 10's captured values: Moonshot answers an
    # invalid bearer with HTTP 401 and
    # {"error": {"message": "Invalid Authentication",
    #            "type": "invalid_authentication_error"}}
    # These tests assert both the real values and the two-step lookup —
    # structured field first, substring fallback — because the fallback is
    # what carries recognition when no structured field reaches the caller.

    def test_the_captured_values_are_the_ones_moonshot_actually_sends(self, runner):
        assert type(runner)._AUTH_ERROR_TYPES == {"invalid_authentication_error"}
        assert "Invalid Authentication" in type(runner)._AUTH_ERROR_PATTERNS

    def test_a_real_moonshot_401_is_recognised(self, runner):
        # The exact payload, fed the way the API backend will feed it.
        assert runner._is_auth_error(
            "invalid_authentication_error", "Invalid Authentication"
        )

    def test_it_is_recognised_from_the_text_alone(self, runner):
        # No structured type survived to the caller — the substring branch
        # has to carry it.
        assert runner._is_auth_error(None, "upstream: Invalid Authentication")

    def test_an_ordinary_tool_failure_is_not_an_auth_error(self, runner):
        assert not runner._is_auth_error("tool_error", "file not found")

    def test_a_structured_type_is_recognised(self, runner, monkeypatch):
        monkeypatch.setattr(type(runner), "_AUTH_ERROR_TYPES", {"auth_failed"})
        assert runner._is_auth_error("auth_failed", "")

    def test_a_text_pattern_is_recognised_without_a_structured_type(
        self, runner, monkeypatch
    ):
        monkeypatch.setattr(type(runner), "_AUTH_ERROR_PATTERNS", ("invalid api key",))
        assert runner._is_auth_error(None, "upstream said: invalid api key here")


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_tools_are_supported_on_both_backends(self, runner):
        assert await runner.supports_tools() is True

    @pytest.mark.asyncio
    async def test_vision_is_declared_false_this_iteration(self, runner):
        # The registry record carries no vision flag and this runner builds no
        # image content blocks. Flipping it needs all three of: a vision model
        # in the registry, image blocks in the request builder, and a new
        # registry field — that last one is a core change for one runner.
        assert await runner.supports_vision() is False
