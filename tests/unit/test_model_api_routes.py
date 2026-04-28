"""Tests for model registry integration with runners and API routes."""

import pytest

import core.registry  # noqa: F401 — force module import before monkeypatch
from core.models_registry import ModelsRegistry


@pytest.fixture
def registry(tmp_path):
    """Create a ModelsRegistry using a temp directory."""
    reg = ModelsRegistry(base_dir=tmp_path)
    return reg


class TestModelRegistrySeeding:
    """Test that runner available_models seeds the registry."""

    def test_claude_runner_seeds_registry(self, registry, monkeypatch):
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        from plugins.claude.runner import ClaudeRunner

        runner = ClaudeRunner({"model": "sonnet"})

        # Seeding now happens inside initialize() (so a fresh install gets
        # the registry populated before any UI access). For unit tests we
        # invoke seed_if_empty directly to mimic that effect.
        registry.seed_if_empty("claude", ClaudeRunner._DEFAULT_MODELS)

        models = runner.available_models

        # Assertions derive from _DEFAULT_MODELS so they keep passing when
        # Anthropic ships new versions and we bump the bundled defaults.
        defaults = ClaudeRunner._DEFAULT_MODELS
        assert len(models) == len(defaults)
        for m in defaults:
            assert (m["id"], m["name"]) in models

        reg_models = registry.get_models("claude")
        assert len(reg_models) == len(defaults)
        for m in defaults:
            assert any(r["api_id"] == m["api_id"] for r in reg_models)

    def test_ollama_runner_seeds_registry(self, registry, monkeypatch):
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        from plugins.ollama.runner import OllamaRunner

        runner = OllamaRunner({"model": "qwen3:8b"})
        models = runner.available_models

        assert len(models) >= 7
        assert ("qwen3:8b", "Qwen3 8B (recommended)") in models

        reg_models = registry.get_models("ollama")
        assert len(reg_models) >= 7

    def test_seed_only_once(self, registry, monkeypatch):
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        from plugins.claude.runner import ClaudeRunner

        runner = ClaudeRunner({"model": "sonnet"})

        # First seed (mimics ClaudeRunner.initialize on a fresh install)
        registry.seed_if_empty("claude", ClaudeRunner._DEFAULT_MODELS)
        assert registry.get_metadata("claude")["source"] == "seed"

        # Manually update — the registry should now reflect the override
        registry.set_models("claude", [{"id": "custom", "name": "Custom"}], "manual")

        # available_models reads from the registry, no implicit re-seed
        models = runner.available_models
        assert models == [("custom", "Custom")]

    def test_claude_model_map_from_registry(self, registry, monkeypatch):
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        # Seed the registry
        registry.set_models(
            "claude",
            [
                {
                    "id": "sonnet",
                    "name": "Sonnet",
                    "api_id": "claude-sonnet-4-5-20250929",
                },
                {"id": "haiku", "name": "Haiku", "api_id": "claude-haiku-4-5-20251001"},
            ],
        )

        from plugins.claude.api_backend import resolve_model

        assert resolve_model("sonnet") == "claude-sonnet-4-5-20250929"
        assert resolve_model("haiku") == "claude-haiku-4-5-20251001"
        # Passthrough for unknown names
        assert resolve_model("claude-opus-4-6") == "claude-opus-4-6"

    def test_claude_model_map_fallback(self, monkeypatch):
        """When registry is empty, falls back to hardcoded defaults."""
        monkeypatch.setattr("core.registry.get_models_registry", lambda: None)

        from plugins.claude.api_backend import _DEFAULT_MODEL_MAP, resolve_model

        # Assert the lookup works against the current map, not against a
        # specific model version, so the test survives default bumps.
        assert resolve_model("sonnet") == _DEFAULT_MODEL_MAP["sonnet"]
        assert resolve_model("haiku") == _DEFAULT_MODEL_MAP["haiku"]
        assert resolve_model("opus") == _DEFAULT_MODEL_MAP["opus"]
        assert resolve_model("unknown-model") == "unknown-model"

    def test_openai_model_map_from_registry(self, registry, monkeypatch):
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        registry.set_models(
            "openai",
            [
                {"id": "gpt-4o", "name": "GPT-4o"},
                {"id": "gpt-5", "name": "GPT-5", "api_id": "gpt-5-0125"},
            ],
        )

        from plugins.openai.api_backend import resolve_model

        # OpenAI models without api_id use id as passthrough
        assert resolve_model("gpt-4o") == "gpt-4o"
        # With api_id, maps to it
        assert resolve_model("gpt-5") == "gpt-5-0125"

    def test_openai_model_map_fallback(self, monkeypatch):
        """When registry is empty, OpenAI passes through model names."""
        monkeypatch.setattr("core.registry.get_models_registry", lambda: None)

        from plugins.openai.api_backend import resolve_model

        assert resolve_model("gpt-4o") == "gpt-4o"
        assert resolve_model("custom-ft-model") == "custom-ft-model"

    def test_registry_get_for_ui_format(self, registry):
        """Verify UI format matches what runners expect."""
        registry.set_models(
            "test",
            [
                {"id": "a", "name": "Model A", "api_id": "full-a-id"},
                {"id": "b", "name": "Model B"},
            ],
        )
        ui_models = registry.get_for_ui("test")
        assert ui_models == [("a", "Model A"), ("b", "Model B")]

    def test_registry_model_map_format(self, registry):
        """Verify model map format for api_backend resolve_model."""
        registry.set_models(
            "test",
            [
                {"id": "short", "name": "Short", "api_id": "full-model-id"},
                {"id": "passthrough", "name": "Pass"},
            ],
        )
        model_map = registry.get_model_map("test")
        assert model_map == {"short": "full-model-id", "passthrough": "passthrough"}
