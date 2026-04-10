"""OpenAI Runner Plugin.

Executes OpenAI models via Chat Completions API or Codex CLI (configurable backend).
API mode uses the openai SDK directly for tool calling and streaming.
CLI mode uses Codex subprocess with JSONL event parsing.

MCP tool access is handled by the MCP Gateway (gridbear-ui):
- Each agent has a pre-provisioned OAuth2 token
- CLI mode: JSON MCP config file points Codex to the gateway SSE endpoint
- API mode: ToolAdapter calls the gateway via HTTP JSON-RPC
- The gateway filters tools based on the agent's mcp_permissions
"""

import os

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse


class OpenAIRunner(BaseRunner):
    """OpenAI runner — dispatches to CLI or API backend."""

    name = "openai"

    def __init__(self, config: dict):
        super().__init__(config)
        self.model = config.get("model", os.getenv("OPENAI_MODEL", "gpt-4.1"))
        self.timeout = config.get("timeout", 120)
        self.max_retries = config.get("max_retries", 2)

        # Backend selection: "api" (default) or "cli" (Codex CLI)
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
        """Initialize runner — delegates to API backend or CLI backend."""
        if self.backend == "api":
            from .api_backend import OpenAIApiBackend

            self._api_backend = OpenAIApiBackend(self.config)
            await self._api_backend.initialize()
            logger.info(
                "OpenAI runner initialized with model %s (backend=api)", self.model
            )
            return

        from .cli_backend import OpenAICliBackend

        self._cli_backend = OpenAICliBackend(self.config)
        logger.info("OpenAI runner initialized with model %s (backend=cli)", self.model)

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._api_backend:
            await self._api_backend.shutdown()
            self._api_backend = None
        self._cli_backend = None

    async def supports_tools(self) -> bool:
        """OpenAI supports function calling."""
        return True

    async def supports_vision(self) -> bool:
        """GPT-4o+ and GPT-4.1+ support vision."""
        return True

    _DEFAULT_MODELS = [
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "gpt-5-mini", "name": "GPT-5 Mini"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini"},
        {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
        {"id": "o3", "name": "o3"},
        {"id": "o3-mini", "name": "o3 Mini"},
        {"id": "o4-mini", "name": "o4-mini"},
    ]

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return OpenAI model choices from registry."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            registry.seed_if_empty("openai", self._DEFAULT_MODELS)
            models = registry.get_for_ui("openai")
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
        """Execute OpenAI model and return response.

        Args:
            prompt: The prompt to send
            session_id: Optional session ID to resume
            progress_callback: Optional async callback for progress messages
            error_callback: Optional async callback for error notifications
            tool_callback: Optional async callback for tool use notifications
            stream_callback: Optional async callback for streaming text
            agent_id: Agent identifier for MCP gateway and logging
            model: Per-agent model override
            no_tools: If True, run without MCP tools
        """
        # Fresh config from DB for hot-reload (per-agent override takes priority)
        effective_model = model
        if not effective_model:
            try:
                from core.orm.model import get_database

                _db = get_database()
                with _db.acquire_sync() as conn:
                    row = conn.execute(
                        "SELECT config FROM app.plugin_registry WHERE name = %s",
                        ("openai",),
                    ).fetchone()
                if row and row["config"]:
                    effective_model = row["config"].get("model", self.model)
            except Exception:
                pass
            if not effective_model:
                effective_model = self.model

        # --- API backend dispatch ---
        if self.backend == "api" and self._api_backend:
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
        msg = f"OpenAI runner: no backend initialized (backend={self.backend})"
        logger.error(msg)
        return RunnerResponse(text=msg, is_error=True)
