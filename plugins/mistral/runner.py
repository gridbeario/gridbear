"""Mistral Runner Plugin.

Executes Mistral models via Chat Completions API or Vibe CLI (configurable backend).
API mode uses httpx directly for tool calling and streaming.
CLI mode uses Vibe subprocess with JSON output parsing.

MCP tool access is handled by the MCP Gateway (gridbear-ui):
- Each agent has a pre-provisioned OAuth2 token
- CLI mode: TOML MCP config points Vibe to the gateway
- API mode: ToolAdapter calls the gateway via HTTP JSON-RPC
- The gateway filters tools based on the agent's mcp_permissions
"""

import os

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse


class MistralRunner(BaseRunner):
    """Mistral runner — dispatches to CLI or API backend."""

    name = "mistral"

    def __init__(self, config: dict):
        super().__init__(config)
        self.model = config.get(
            "model", os.getenv("MISTRAL_MODEL", "mistral-large-latest")
        )
        self.timeout = config.get("timeout", 120)
        self.max_retries = config.get("max_retries", 2)

        # Backend selection: "api" (default) or "cli" (Vibe CLI)
        self.backend = config.get("backend", "api")
        self._api_backend = None
        self._cli_backend = None

        # MCP tool notification flags
        self.notify_tool_use = config.get("notify_tool_use", True)

        # Callbacks for feedback and error notifications
        self._progress_callback = None
        self._error_callback = None

    def set_progress_callback(self, callback):
        """Set callback for progress notifications."""
        self._progress_callback = callback

    def set_error_callback(self, callback):
        """Set callback for error notifications."""
        self._error_callback = callback

    async def initialize(self) -> None:
        """Initialize runner — delegates to API, Codestral, or CLI backend."""
        if self.backend in ("api", "codestral"):
            from .api_backend import CODESTRAL_API_BASE, MistralApiBackend

            if self.backend == "codestral":
                codestral_config = dict(self.config)
                codestral_config.setdefault("model", "codestral-latest")
                self._api_backend = MistralApiBackend(
                    codestral_config,
                    base_url=CODESTRAL_API_BASE,
                    api_key_name="CODESTRAL_API_KEY",
                )
            else:
                self._api_backend = MistralApiBackend(self.config)

            await self._api_backend.initialize()
            logger.info(
                "Mistral runner initialized with model %s (backend=%s)",
                self.model,
                self.backend,
            )
            return

        from .cli_backend import MistralCliBackend

        self._cli_backend = MistralCliBackend(self.config)
        logger.info(
            "Mistral runner initialized with model %s (backend=cli)", self.model
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._api_backend:
            await self._api_backend.shutdown()
            self._api_backend = None
        self._cli_backend = None

    async def supports_tools(self) -> bool:
        """Mistral supports function calling."""
        return True

    async def supports_vision(self) -> bool:
        """Only Pixtral models support vision."""
        vision_prefixes = ("pixtral",)
        return self.model.startswith(vision_prefixes)

    _DEFAULT_MODELS = [
        {"id": "mistral-large-latest", "name": "Mistral Large"},
        {"id": "mistral-medium-latest", "name": "Mistral Medium"},
        {"id": "mistral-small-latest", "name": "Mistral Small"},
        {"id": "devstral-2-latest", "name": "Devstral 2"},
        {"id": "codestral-latest", "name": "Codestral"},
        {"id": "pixtral-large-latest", "name": "Pixtral Large"},
        {"id": "pixtral-latest", "name": "Pixtral"},
        {"id": "mistral-nemo", "name": "Mistral Nemo"},
    ]

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return Mistral model choices from registry."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            registry.seed_if_empty("mistral", self._DEFAULT_MODELS)
            models = registry.get_for_ui("mistral")
            if models:
                return models
        return [(m["id"], m["name"]) for m in self._DEFAULT_MODELS]

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
        agent_id: str | None = None,
        model: str | None = None,
        no_tools: bool = False,
        **kwargs,
    ) -> RunnerResponse:
        """Execute Mistral model and return response."""
        # Fresh config from DB for hot-reload (per-agent override takes priority)
        effective_model = model
        if not effective_model:
            try:
                from core.orm.model import get_database

                _db = get_database()
                with _db.acquire_sync() as conn:
                    row = conn.execute(
                        "SELECT config FROM app.plugin_registry WHERE name = %s",
                        ("mistral",),
                    ).fetchone()
                if row and row["config"]:
                    effective_model = row["config"].get("model", self.model)
            except Exception:
                pass
            if not effective_model:
                effective_model = self.model

        # --- API / Codestral backend dispatch ---
        if self.backend in ("api", "codestral") and self._api_backend:
            return await self._api_backend.run(
                prompt=prompt,
                session_id=session_id,
                progress_callback=progress_callback,
                error_callback=error_callback,
                tool_callback=tool_callback,
                stream_callback=stream_callback,
                agent_id=agent_id,
                model=effective_model,
                no_tools=no_tools,
                **kwargs,
            )

        # --- CLI backend dispatch ---
        if self.backend == "cli" and self._cli_backend:
            return await self._cli_backend.run(
                prompt=prompt,
                session_id=session_id,
                progress_callback=progress_callback,
                error_callback=error_callback,
                tool_callback=tool_callback,
                stream_callback=stream_callback,
                agent_id=agent_id,
                model=effective_model,
                no_tools=no_tools,
                **kwargs,
            )

        # Fallback: no backend initialized
        msg = f"Mistral runner: no backend initialized (backend={self.backend})"
        logger.error(msg)
        return RunnerResponse(text=msg, is_error=True)
