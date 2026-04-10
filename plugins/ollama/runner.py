"""Ollama Runner Plugin.

Executes local LLM models via Ollama's native /api/chat endpoint.
API-only (no CLI backend) — uses httpx directly against Ollama.

MCP tool access is handled by the MCP Gateway (gridbear-ui):
- Each agent has a pre-provisioned OAuth2 token
- ToolAdapter calls the gateway via HTTP JSON-RPC
- The gateway filters tools based on the agent's mcp_permissions
"""

import os

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse


class OllamaRunner(BaseRunner):
    """Ollama runner — local LLM inference via native API."""

    name = "ollama"

    def __init__(self, config: dict):
        super().__init__(config)
        self.host = os.getenv("OLLAMA_URL") or config.get(
            "host", "http://localhost:11434"
        )
        self.model = config.get("model", "qwen3:8b")
        self.timeout = config.get("timeout", 300)
        self._api_backend = None

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
        """Initialize runner — delegates to API backend."""
        from .api_backend import OllamaApiBackend

        self._api_backend = OllamaApiBackend(self.config)
        await self._api_backend.initialize()
        logger.info(
            "Ollama runner initialized (host=%s, model=%s)", self.host, self.model
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._api_backend:
            await self._api_backend.shutdown()
            self._api_backend = None

    async def supports_tools(self) -> bool:
        """Ollama supports native function calling."""
        return True

    async def supports_vision(self) -> bool:
        """Vision not supported yet (would need LLaVA or similar)."""
        return False

    _DEFAULT_MODELS = [
        {"id": "qwen3:8b", "name": "Qwen3 8B (recommended)"},
        {"id": "qwen3:0.6b", "name": "Qwen3 0.6B (fastest)"},
        {"id": "qwen3.5:397b-cloud", "name": "Qwen3.5 397B (cloud)"},
        {"id": "qwen2.5:7b", "name": "Qwen2.5 7B"},
        {"id": "devstral-small:24b-cloud", "name": "Devstral Small 24B (cloud)"},
        {"id": "llama3.1:8b", "name": "Llama 3.1 8B"},
        {"id": "llama3.2:3b", "name": "Llama 3.2 3B"},
        {"id": "mistral:7b", "name": "Mistral 7B"},
        {"id": "phi4-mini:3.8b", "name": "Phi-4 Mini 3.8B"},
    ]

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return Ollama model choices from registry."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            registry.seed_if_empty("ollama", self._DEFAULT_MODELS)
            models = registry.get_for_ui("ollama")
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
        """Execute Ollama model and return response.

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
        if not self._api_backend:
            msg = "Ollama runner: API backend not initialized"
            logger.error(msg)
            return RunnerResponse(text=msg, is_error=True)

        # Fresh config from DB for hot-reload (per-agent override takes priority)
        effective_model = model
        if not effective_model:
            try:
                from core.orm.model import get_database

                _db = get_database()
                with _db.acquire_sync() as conn:
                    row = conn.execute(
                        "SELECT config FROM app.plugin_registry WHERE name = %s",
                        ("ollama",),
                    ).fetchone()
                if row and row["config"]:
                    effective_model = row["config"].get("model", self.model)
            except Exception:
                pass
            if not effective_model:
                effective_model = self.model

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
