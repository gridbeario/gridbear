"""Kimi Runner Plugin.

Executes Kimi (Moonshot AI) via the Kimi Code CLI or the Moonshot HTTP API.
The backend is read from config and its module imported lazily, so an
API-only deployment never loads the process pool.

MCP tool access goes through the MCP Gateway as it does for every other
runner: the per-agent config file written by MCPTokenManager is passed to
the CLI by path, never inline on a command line where any process in the
container could read the bearer token from /proc/*/cmdline.

This plugin imports nothing from other plugins. Where code is shared in
spirit with plugins/claude it has been copied, and the divergences are
enumerated in the design doc's Pooling section.

This iteration ships the API backend only. The Kimi CLI at 2.0.0 has none of
the flags the original design assumed (--input-format, --config-file,
--mcp-config, --work-dir) and needs Node >= 22 while the container runs Node
20 — see docs/plans/kimi-cli-facts.md. The CLI backend and its config/pool
machinery are out of scope for this task; run() still raises until the API
backend lands.
"""

import os
import time

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse  # noqa: F401

# Module-level auth error tracking, read by this plugin's API routes.
# Both run in the same gridbear process.
_last_auth_error_at: float = 0.0


def get_auth_error_info() -> dict | None:
    """Return auth error info if a failure occurred within the last hour."""
    if _last_auth_error_at and (time.time() - _last_auth_error_at) < 3600:
        return {"timestamp": _last_auth_error_at}
    return None


class KimiRunner(BaseRunner):
    """Kimi runner — dispatches to the CLI or the Moonshot API backend."""

    name = "kimi"

    # Row 10 of docs/plans/kimi-cli-facts.md is "NOT CAPTURED — needs a turn
    # against a deliberately invalid credential", which needs a Moonshot API
    # key this plugin cannot yet obtain (that key is entered through the
    # plugin page this task creates, at /plugins/kimi). Left empty rather
    # than guessed: an invented pattern that never matches is worse than an
    # empty one that obviously does not. _is_auth_error's substring branch
    # carries no recognition power until this is filled in from a real
    # captured error.
    _AUTH_ERROR_TYPES: set[str] = set()
    _AUTH_ERROR_PATTERNS: tuple[str, ...] = ()

    # Row 9 of docs/plans/kimi-cli-facts.md is also "NOT CAPTURED — needs a
    # credential for GET /v1/models, or `kimi provider catalog`". These two
    # entries are provisional, plausible-but-unconfirmed ids, not the row 9
    # data. `api_id` is written explicitly even where it equals `id`,
    # because the two spaces must stay separable. row 9 replaces this list
    # verbatim once captured; until then `placeholder: True` is what makes
    # `_refuse_placeholder_models()` (and therefore `initialize()`) refuse
    # to let this runner start.
    _DEFAULT_MODELS: list[dict] = [
        {
            "id": "kimi-k2-turbo",
            "name": "Kimi K2 Turbo",
            "api_id": "kimi-k2-turbo",
            "placeholder": True,
        },
        {
            "id": "kimi-k2",
            "name": "Kimi K2",
            "api_id": "kimi-k2",
            "placeholder": True,
        },
    ]

    def __init__(self, config: dict):
        super().__init__(config)
        self.backend = config.get("backend", "api")
        self.model = config.get("model", os.getenv("KIMI_MODEL", "kimi-k2-turbo"))
        self.base_url = config.get(
            "base_url", os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        )
        self.timeout = config.get("timeout", 900)
        self.max_retries = config.get("max_retries", 2)
        # CLI-internal tools. Empty means none are enabled, not unrestricted:
        # the gateway ACL does not see the CLI's own built-in tools.
        self.allowed_tools = config.get("allowed_tools", [])
        self.use_pool = config.get("use_pool", False)
        self.pool_max_processes = config.get("pool_max_processes", 2)
        self.pool_max_requests = config.get("pool_max_requests", 100)
        self.pool_idle_timeout = config.get("pool_idle_timeout", 300)
        self.pool_max_age = config.get("pool_max_age", 3600)
        self.notify_tool_use = config.get("notify_tool_use", True)

        from config.settings import ATTACHMENTS_DIR, BASE_DIR, VERBOSE_AGENT_LOG

        self.working_dir = BASE_DIR
        self.attachments_dir = ATTACHMENTS_DIR
        self.verbose = VERBOSE_AGENT_LOG

        self._pool = None
        self._api_backend = None
        self._progress_callback = None
        self._error_callback = None

    def set_progress_callback(self, callback):
        """Callback signature: async def callback(message: str)."""
        self._progress_callback = callback

    def set_error_callback(self, callback):
        """Callback signature: async def callback(error_type: str, details: dict)."""
        self._error_callback = callback

    def _is_auth_error(self, error_type: str | None, text: str) -> bool:
        """Recognise an authentication failure.

        Structured field first, substring fallback second, because the CLI may
        not always provide a structured error field. Same two-step lookup the
        repo already uses; the values are Kimi's own, since the two CLIs do
        not share an error vocabulary.
        """
        if error_type and error_type in self._AUTH_ERROR_TYPES:
            return True
        return any(p in text for p in self._AUTH_ERROR_PATTERNS)

    @staticmethod
    def _refuse_placeholder_models(registry) -> None:
        """Refuse to start on model ids nobody confirmed.

        `placeholder: true` is a marker this plugin writes; no operator action
        through the models page produces it. So a machine serving a marked
        entry is running on invented ids — and seed_if_empty() preserves them
        for ever once the file exists, which is why the message names the file
        and the page that can rewrite it rather than suggesting a reinstall.
        """
        marked = [m["id"] for m in registry.get_models("kimi") if m.get("placeholder")]
        if marked:
            raise RuntimeError(
                f"Kimi model registry serves unconfirmed placeholder ids {marked}. "
                f"Fix data/models/kimi.json from the models page at /plugins/kimi "
                f"before enabling this runner."
            )

    async def initialize(self) -> None:
        """Seed the registry, then run the gates before any turn can start."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            # Idempotent: writes only when data/models/kimi.json is absent.
            registry.seed_if_empty("kimi", self._DEFAULT_MODELS)
            self._refuse_placeholder_models(registry)

        logger.info(
            "Kimi runner initialized with model %s (backend=%s)",
            self.model,
            self.backend,
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._api_backend:
            await self._api_backend.shutdown()
            self._api_backend = None
        if self._pool:
            await self._pool.stop()
            self._pool = None

    async def supports_tools(self) -> bool:
        """Both backends run the tool loop."""
        return True

    async def supports_vision(self) -> bool:
        """Declared constant, False this iteration.

        The registry record carries no vision flag — get_for_ui() projects
        only id and name, and no data/models/*.json has such a field — and
        this runner builds no image content blocks. It is not derived from
        the (id, label) list, which carries no such bit.
        """
        return False

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return Kimi model choices from the registry.

        Both branches return list[tuple[str, str]]: the fallback projects
        _DEFAULT_MODELS into the registry's shape. Returning the dicts
        instead would break the dropdown this fallback exists to protect,
        and only on a fresh install.
        """
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            models = registry.get_for_ui("kimi")
            if models:
                return models
        return [(m["id"], m["name"]) for m in self._DEFAULT_MODELS]

    async def run(
        self,
        prompt,
        session_id=None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
        agent_id=None,
        use_pool=None,
        model=None,
        no_tools=False,
        **kwargs,
    ) -> RunnerResponse:
        """Dispatch to the configured backend. Filled in by Tasks 11 and 14."""
        raise NotImplementedError("backends land in Task 11 (CLI) and Task 14 (API)")
