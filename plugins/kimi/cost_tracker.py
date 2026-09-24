"""Cost tracking for Moonshot Kimi usage.

Prices are per million tokens and the table is indexed by **api_id** — what
the provider bills — never by the UI id that --model carries. The two spaces
are separate by design; conflating them prices every model whose ids differ
at zero, and calculate_cost returns 0.0 for an unknown model without
raising, so nothing anywhere would report it.

Rates recorded in docs/plans/kimi-cli-facts.md row 9.
"""

import asyncio

from config.logging_config import logger
from core.runners.cost_calculator import calculate_cost as _calculate_cost

# (api_id_prefix, input_$/M, output_$/M)
# Matching uses startswith(), so more specific prefixes come first. Neither
# id is a prefix of the other here ("kimi-k2.6" vs "kimi-k2.7-code" diverge
# at the 8th character), so this ordering is not load-bearing today — kept
# alphabetical for readability, not correctness.
#
# Cache-hit input is cheaper for both models ($0.16 / $0.19 per 1M) but is
# deliberately not modelled: core/runners/cost_calculator.py takes a single
# input rate per prefix, and threading a second rate through it would mean
# changing a core module for one runner. The cache-miss rate is used
# unconditionally, so a cached turn is over-costed, never under-costed.
KIMI_PRICING: list[tuple[str, float, float]] = [
    ("kimi-k2.6", 0.95, 4.00),
    ("kimi-k2.7-code", 0.95, 4.00),
]

# One notification per api_id for the lifetime of the process, not one per
# turn. The ledger is keyed on the api_id, and the two repairs clear it
# differently — worth knowing, because only one of them needs a restart.
#
# Editing KIMI_PRICING above is a module constant: the process must restart
# before the new rate is read, and that restart also empties this set.
# Correcting a mistyped api_id in data/models/kimi.json needs no restart —
# ModelsRegistry._load re-reads the file on every call
# (core/models_registry.py:41-48, no caching), so resolve_api_id picks the
# fix up on the next turn, and the stale entry here does not block it
# because the corrected api_id is a different string.
_notified_unpriced: set[str] = set()


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost. `model` must be an api_id, not a UI id."""
    return _calculate_cost(model, KIMI_PRICING, input_tokens, output_tokens)


def is_priced(api_id: str) -> bool:
    """Whether the table can price this api_id at all.

    calculate_cost cannot answer this: it returns 0.0 both for a free model
    and for one it has never heard of. This is what separates the two.
    """
    return any(api_id.startswith(prefix) for prefix, _, _ in KIMI_PRICING)


def resolve_api_id(ui_id: str) -> str:
    """Resolve a UI id to the api_id the provider bills.

    Falls back to the ui_id itself, matching get_model_map()
    (core/models_registry.py:83-89) for entries with no explicit api_id.
    """
    from core.registry import get_models_registry

    registry = get_models_registry()
    if registry:
        return registry.get_model_map("kimi").get(ui_id, ui_id)
    return ui_id


def _send_unpriced_notification(ui_id: str, api_id: str) -> None:
    """Tell the operator a turn was priced at zero, and name the two fixes."""
    from core.notifications_client import send_notification

    asyncio.ensure_future(
        send_notification(
            category="runner_error",
            severity="error",
            title=f"Kimi: no price for model {ui_id}",
            message=(
                f"Model {ui_id} resolves to api_id {api_id}, which is absent "
                f"from the Kimi price table, so turns on it are recorded at "
                f"$0.00. Either add the rate to plugins/kimi/cost_tracker.py, "
                f"or correct the api_id in data/models/kimi.json from the "
                f"models page."
            ),
            source="kimi",
            action_url="/plugins/kimi",
        )
    )


def cost_for_turn(ui_id: str, input_tokens: int, output_tokens: int) -> float:
    """Cost a turn from its UI model id. The entry point the runner calls.

    An unpriced model does not fail the turn and does not fail startup: the
    registry is operator-writable, so an operator adding a genuine new
    Moonshot model from the models page reaches this path through a
    legitimate action on correct data. Refusing to start there would take the
    runner down for every agent because somebody added a row in the UI. So:
    one notification per model id, and the turn completes.
    """
    api_id = resolve_api_id(ui_id)
    if not is_priced(api_id) and api_id not in _notified_unpriced:
        _notified_unpriced.add(api_id)
        logger.warning(
            "Kimi model %s (api_id %s) has no rate — turn recorded at $0.00",
            ui_id,
            api_id,
        )
        _send_unpriced_notification(ui_id, api_id)
    return calculate_cost(api_id, input_tokens, output_tokens)
