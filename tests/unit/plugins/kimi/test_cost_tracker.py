"""Pricing is indexed by api_id, never by UI id.

calculate_cost matches by startswith and returns 0.0 for an unknown model
without raising (core/runners/cost_calculator.py:38), so a turn on an
unpriced model is not an error anywhere — it is a silently free turn. These
tests are the difference between that and a reported gap.
"""

import pytest

import core.registry  # noqa: F401 — force module import before monkeypatch
from core.models_registry import ModelsRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = ModelsRegistry(base_dir=tmp_path)
    monkeypatch.setattr("core.registry.get_models_registry", lambda: reg)
    return reg


@pytest.fixture(autouse=True)
def clear_notification_ledger():
    from plugins.kimi import cost_tracker

    cost_tracker._notified_unpriced.clear()
    yield
    cost_tracker._notified_unpriced.clear()


class TestPriceTable:
    def test_every_shipped_default_model_is_priced(self):
        from plugins.kimi.cost_tracker import is_priced
        from plugins.kimi.runner import KimiRunner

        assert KimiRunner._DEFAULT_MODELS, "row 9 has not been transcribed"
        for model in KimiRunner._DEFAULT_MODELS:
            assert is_priced(model["api_id"]), f"{model['api_id']} has no rate"

    def test_a_priced_model_costs_more_than_zero(self):
        from plugins.kimi.cost_tracker import KIMI_PRICING, calculate_cost

        prefix = KIMI_PRICING[0][0]
        assert calculate_cost(prefix, 1_000_000, 1_000_000) > 0

    def test_an_unknown_model_costs_zero_without_raising(self):
        from plugins.kimi.cost_tracker import calculate_cost

        assert calculate_cost("no-such-model", 1_000_000, 1_000_000) == 0.0


class TestIdResolution:
    def test_the_ui_id_is_resolved_to_the_billed_api_id(self, registry):
        from plugins.kimi.cost_tracker import resolve_api_id

        registry.set_models(
            "kimi", [{"id": "turbo", "name": "Turbo", "api_id": "kimi-billed-turbo"}]
        )
        assert resolve_api_id("turbo") == "kimi-billed-turbo"

    def test_an_unmapped_id_resolves_to_itself(self, registry):
        from plugins.kimi.cost_tracker import resolve_api_id

        registry.set_models("kimi", [])
        assert resolve_api_id("standalone") == "standalone"

    def test_costing_uses_the_api_id_not_the_ui_id(self, registry, monkeypatch):
        # The defect this guards: costing the UI id prices every model whose
        # ids differ at zero, silently.
        from plugins.kimi import cost_tracker

        monkeypatch.setattr(
            cost_tracker, "KIMI_PRICING", [("billed-x", 10.0, 30.0)], raising=True
        )
        registry.set_models("kimi", [{"id": "ui-x", "name": "X", "api_id": "billed-x"}])
        assert cost_tracker.cost_for_turn("ui-x", 1_000_000, 0) == pytest.approx(10.0)


class TestUnpricedNotification:
    def test_an_unpriced_model_completes_the_turn_at_zero(self, registry, monkeypatch):
        from plugins.kimi import cost_tracker

        sent = []
        monkeypatch.setattr(
            cost_tracker, "_send_unpriced_notification", lambda *a: sent.append(a)
        )
        registry.set_models(
            "kimi", [{"id": "new", "name": "New", "api_id": "unpriced-new"}]
        )
        assert cost_tracker.cost_for_turn("new", 1_000, 1_000) == 0.0
        assert len(sent) == 1

    def test_it_notifies_once_per_model_id_not_once_per_turn(
        self, registry, monkeypatch
    ):
        from plugins.kimi import cost_tracker

        sent = []
        monkeypatch.setattr(
            cost_tracker, "_send_unpriced_notification", lambda *a: sent.append(a)
        )
        registry.set_models(
            "kimi", [{"id": "new", "name": "New", "api_id": "unpriced-new"}]
        )
        for _ in range(5):
            cost_tracker.cost_for_turn("new", 1_000, 1_000)
        assert len(sent) == 1

    def test_two_different_unpriced_models_notify_separately(
        self, registry, monkeypatch
    ):
        from plugins.kimi import cost_tracker

        sent = []
        monkeypatch.setattr(
            cost_tracker, "_send_unpriced_notification", lambda *a: sent.append(a)
        )
        registry.set_models(
            "kimi",
            [
                {"id": "a", "name": "A", "api_id": "unpriced-a"},
                {"id": "b", "name": "B", "api_id": "unpriced-b"},
            ],
        )
        cost_tracker.cost_for_turn("a", 1, 1)
        cost_tracker.cost_for_turn("b", 1, 1)
        assert len(sent) == 2

    def test_a_priced_model_notifies_nothing(self, registry, monkeypatch):
        from plugins.kimi import cost_tracker

        sent = []
        monkeypatch.setattr(
            cost_tracker, "_send_unpriced_notification", lambda *a: sent.append(a)
        )
        monkeypatch.setattr(
            cost_tracker, "KIMI_PRICING", [("billed-x", 10.0, 30.0)], raising=True
        )
        registry.set_models("kimi", [{"id": "ui-x", "name": "X", "api_id": "billed-x"}])
        cost_tracker.cost_for_turn("ui-x", 1_000, 1_000)
        assert sent == []
