"""Claude Runner Plugin.

Executes Claude via CLI or Anthropic API (configurable backend).
CLI mode uses process pooling for faster response times.
API mode uses the anthropic SDK directly for lower latency.

MCP tool access is handled by the MCP Gateway (gridbear-ui):
- Each agent has a pre-provisioned OAuth2 token
- CLI mode: static MCP config file points Claude CLI to the gateway SSE endpoint
- API mode: ToolAdapter calls the gateway via HTTP JSON-RPC
- The gateway filters tools based on the agent's mcp_permissions
"""

import asyncio
import os
import time
from pathlib import Path

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse
from core.response_parser import parse_claude_output

# Module-level auth error tracking — shared between runner and API routes
# (both run in the same gridbear process).
_last_auth_error_at: float = 0.0


def get_auth_error_info() -> dict | None:
    """Return auth error info if a recent failure occurred (within 60 min)."""
    if _last_auth_error_at and (time.time() - _last_auth_error_at) < 3600:
        return {"timestamp": _last_auth_error_at}
    return None


class ClaudeRunner(BaseRunner):
    """Claude AI runner — dispatches to CLI or API backend."""

    name = "claude"
    _AUTH_ERROR_TYPES = {"authentication_failed", "authentication_error"}
    _AUTH_ERROR_PATTERNS = ("authentication_error", "Invalid bearer token")

    def __init__(self, config: dict):
        super().__init__(config)
        self.model = config.get("model", os.getenv("CLAUDE_MODEL", "sonnet"))
        self.timeout = config.get(
            "timeout", int(os.getenv("CLAUDE_TIMEOUT_SECONDS", "900"))
        )
        self.max_retries = config.get("max_retries", 2)
        # Send "processing" message after this many seconds
        self.feedback_delay = config.get("feedback_delay", 30)

        # Backend selection: "cli" (default) or "api" (Anthropic SDK)
        self.backend = config.get("backend", "cli")
        self._api_backend = None

        # Process pooling configuration (CLI mode only)
        self.use_pool = config.get("use_pool", False)
        self.pool_max_processes = config.get("pool_max_processes", 2)
        self.pool_max_requests = config.get("pool_max_requests", 100)
        self.pool_idle_timeout = config.get("pool_idle_timeout", 300)

        # MCP tool notification flags (default True for backward compat)
        self.notify_tool_use = config.get("notify_tool_use", True)
        self.log_mcp_calls = config.get("log_mcp_calls", True)
        self.log_mcp_input = config.get("log_mcp_input", True)
        self.log_mcp_output = config.get("log_mcp_output", True)

        # Paths from settings
        from config.settings import ATTACHMENTS_DIR, BASE_DIR, VERBOSE_AGENT_LOG

        self.working_dir = BASE_DIR
        self.attachments_dir = ATTACHMENTS_DIR
        self.verbose = VERBOSE_AGENT_LOG

        # Process pool (initialized lazily)
        self._pool = None

        # Callbacks for feedback and error notifications
        self._progress_callback = None
        self._error_callback = None

    def set_progress_callback(self, callback):
        """Set callback for progress notifications.

        Callback signature: async def callback(message: str)
        """
        self._progress_callback = callback

    def set_error_callback(self, callback):
        """Set callback for error notifications.

        Callback signature: async def callback(error_type: str, details: dict)
        """
        self._error_callback = callback

    def _is_auth_error(self, error_type: str | None, text: str) -> bool:
        """Check if the error is an authentication failure.

        Checks both structured error_type and response text, since the
        CLI may not always provide a structured error field.
        """
        if error_type and error_type in self._AUTH_ERROR_TYPES:
            return True
        return any(p in text for p in self._AUTH_ERROR_PATTERNS)

    def _notify_auth_failure(self):
        """Fire-and-forget admin notification for CLI auth failures."""
        global _last_auth_error_at
        _last_auth_error_at = time.time()

        from core.notifications_client import send_notification

        asyncio.ensure_future(
            send_notification(
                category="runner_error",
                severity="error",
                title="Claude CLI: authentication failed",
                message=(
                    "The Claude CLI authentication token has expired "
                    "or is invalid. Re-authenticate from the plugin "
                    "configuration page."
                ),
                source="claude",
                action_url="/plugins/claude",
            )
        )

    async def initialize(self) -> None:
        """Initialize runner — delegates to API backend or CLI pool."""
        # Generate CLI config files (settings.local.json, .claude.json)
        # into the named volume on each startup
        from .config_generator import generate_all

        generate_all()

        if self.backend == "api":
            from .api_backend import ClaudeApiBackend

            self._api_backend = ClaudeApiBackend(self.config)
            await self._api_backend.initialize()
            logger.info(
                "Claude runner initialized with model %s (backend=api)", self.model
            )
            return

        if self.use_pool:
            from .process_pool import ClaudeProcessPool

            self._pool = ClaudeProcessPool(
                max_processes_per_agent=self.pool_max_processes,
                max_requests_per_process=self.pool_max_requests,
                idle_timeout_seconds=self.pool_idle_timeout,
                working_dir=self.working_dir,
            )
            await self._pool.start()
            logger.info(
                "Claude runner initialized with model %s (backend=cli, pool=on)",
                self.model,
            )
        else:
            logger.info(
                "Claude runner initialized with model %s (backend=cli)", self.model
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
        """Claude supports MCP tools."""
        return True

    async def supports_vision(self) -> bool:
        """Claude supports vision."""
        return True

    _DEFAULT_MODELS = [
        {"id": "opus", "name": "Opus", "api_id": "claude-opus-4-6-20250827"},
        {"id": "sonnet", "name": "Sonnet", "api_id": "claude-sonnet-4-5-20250929"},
        {"id": "haiku", "name": "Haiku", "api_id": "claude-haiku-4-5-20251001"},
    ]

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return Claude model choices from registry."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            registry.seed_if_empty("claude", self._DEFAULT_MODELS)
            models = registry.get_for_ui("claude")
            if models:
                return models
        return [(m["id"], m["name"]) for m in self._DEFAULT_MODELS]

    def _get_mcp_config_path(self, agent_id: str | None) -> Path | None:
        """Get the static MCP config path for an agent from the token manager."""
        if not agent_id:
            return None

        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        if not tm:
            return None

        path = tm.get_config_path(agent_id)
        if path:
            return Path(path)
        return None

    def _get_file_permissions(self) -> list[str]:
        """Get allowed tool permissions from SystemConfig (file ops + MCP gateway)."""
        try:
            from core.system_config import SystemConfig

            settings = SystemConfig.get_param_sync("claude_settings")
            if not settings:
                return []
            return settings.get("permissions", {}).get("allow", [])
        except Exception as e:
            logger.warning(f"Failed to load permissions: {e}")
            return []

    def _get_mcp_allowed_tools(self, agent_id: str | None) -> list[str]:
        """Get pre-authorized MCP tool names for an agent from the token manager."""
        if not agent_id:
            return []

        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        if not tm:
            return []

        return tm.get_allowed_tools(agent_id)

    async def _send_progress_feedback(self, delay: int):
        """Send progress feedback after delay seconds (uses instance callback)."""
        await asyncio.sleep(delay)
        if self._progress_callback:
            try:
                await self._progress_callback(
                    "Sto ancora elaborando la richiesta, potrebbe richiedere più tempo..."
                )
            except Exception as e:
                logger.warning(f"Failed to send progress feedback: {e}")

    async def _send_progress_feedback_with_callback(self, delay: int, callback):
        """Send progress feedback after delay seconds using provided callback."""
        await asyncio.sleep(delay)
        if callback:
            try:
                await callback(
                    "Sto ancora elaborando la richiesta, potrebbe richiedere più tempo..."
                )
            except Exception as e:
                logger.warning(f"Failed to send progress feedback: {e}")

    async def _notify_error(self, error_type: str, details: dict):
        """Notify about critical errors (uses instance callback)."""
        if self._error_callback:
            try:
                await self._error_callback(error_type, details)
            except Exception as e:
                logger.warning(f"Failed to send error notification: {e}")

    async def _notify_error_with_callback(
        self, callback, error_type: str, details: dict
    ):
        """Notify about critical errors using provided callback."""
        if callback:
            try:
                await callback(error_type, details)
            except Exception as e:
                logger.warning(f"Failed to send error notification: {e}")

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
        agent_id: str | None = None,
        use_pool: bool | None = None,
        model: str | None = None,
        no_tools: bool = False,
        **kwargs,
    ) -> RunnerResponse:
        """Execute Claude Code CLI and return parsed response.

        Args:
            prompt: The prompt to send to Claude
            session_id: Optional session ID to resume
            progress_callback: Optional async callback for progress messages.
                              Signature: async def callback(message: str)
            error_callback: Optional async callback for error notifications.
                           Signature: async def callback(error_type: str, details: dict)
            tool_callback: Optional async callback for tool use notifications.
                          Signature: async def callback(tool_name: str, tool_input: dict)
            stream_callback: Optional async callback for streaming text updates.
                            Signature: async def callback(text: str)
            agent_id: Agent identifier (required for process pooling and MCP gateway)
            use_pool: Override pool usage for this call. None = use runner default.
            model: Per-agent model override. None = use runner default (self.model).
            no_tools: If True, run without MCP tools (simple prompt → answer).
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
                        ("claude",),
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

        # --- CLI backend (existing logic) ---
        # Resolve effective model: agent override > runner default (already resolved above)

        # Use per-call callbacks if provided, otherwise fall back to instance callbacks
        progress_cb = progress_callback or self._progress_callback
        error_cb = error_callback or self._error_callback

        # Get MCP config path from token manager (static per agent)
        mcp_config_path = self._get_mcp_config_path(agent_id)

        # Determine if we should use pooling (never pool no_tools calls)
        should_use_pool = use_pool if use_pool is not None else self.use_pool

        # Extract user identity for MCP gateway side-channel
        unified_id = kwargs.get("unified_id")

        # Use pool if enabled and available (skip for no_tools)
        if should_use_pool and self._pool and agent_id and not no_tools:
            return await self._run_with_pool(
                agent_id=agent_id,
                prompt=prompt,
                session_id=session_id,
                mcp_config_path=mcp_config_path,
                tool_callback=tool_callback,
                stream_callback=stream_callback,
                model=effective_model,
                unified_id=unified_id,
            )

        # Fall back to subprocess mode
        feedback_task = None

        # Tell MCP gateway which user is making this request
        if unified_id:
            await self._set_user_context(agent_id or "", unified_id)

        try:
            cmd = self._build_command(
                prompt,
                session_id,
                mcp_config_path,
                agent_id,
                model=effective_model,
                no_tools=no_tools,
            )
            logger.debug(f"Running command: {' '.join(cmd)}")

            # Always log prompt size for diagnostics
            prompt_bytes = len(prompt.encode("utf-8"))
            logger.info(
                f"Prompt size: {prompt_bytes} bytes "
                f"({prompt_bytes / 1024:.1f} KB, "
                f"{prompt.count(chr(10))} lines)"
            )

            # Dump prompt to file for inspection (toggle via system_config)
            try:
                from core.system_config import SystemConfig

                if SystemConfig.get_param_sync("dump_prompts"):
                    dump_dir = Path("/app/data/prompt_dumps")
                    dump_dir.mkdir(exist_ok=True)
                    from datetime import datetime

                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    agent_label = agent_id or "unknown"
                    dump_path = dump_dir / f"{agent_label}_{ts}.txt"
                    dump_path.write_text(prompt, encoding="utf-8")
                    logger.info(f"Prompt dumped to {dump_path}")
            except Exception:
                pass

            # Verbose logging: log full prompt
            if self.verbose:
                logger.info("=" * 60)
                logger.info("[VERBOSE] PROMPT TO CLAUDE:")
                logger.info("-" * 60)
                for line in prompt.split("\n")[:100]:  # First 100 lines
                    logger.info(f"  {line}")
                if prompt.count("\n") > 100:
                    logger.info(f"  ... ({prompt.count(chr(10)) - 100} more lines)")
                logger.info("=" * 60)

            response: RunnerResponse | None = None
            for attempt in range(self.max_retries + 1):
                # Start feedback task for first attempt
                if attempt == 0 and progress_cb and self.feedback_delay > 0:
                    feedback_task = asyncio.create_task(
                        self._send_progress_feedback_with_callback(
                            self.feedback_delay, progress_cb
                        )
                    )

                try:
                    result = await self._run_subprocess(cmd, stdin_data=prompt)

                    # Cancel feedback task if still pending
                    if feedback_task and not feedback_task.done():
                        feedback_task.cancel()
                        try:
                            await feedback_task
                        except asyncio.CancelledError:
                            pass

                    parsed = parse_claude_output(result)
                    response = RunnerResponse(
                        text=parsed.text,
                        session_id=parsed.session_id,
                        cost_usd=parsed.cost_usd,
                        is_error=parsed.is_error,
                        raw=parsed.raw,
                    )

                    # Verbose logging: log response
                    if self.verbose:
                        logger.info("=" * 60)
                        logger.info(
                            f"[VERBOSE] RESPONSE FROM CLAUDE (cost: ${parsed.cost_usd:.4f}, error: {parsed.is_error}):"
                        )
                        logger.info("-" * 60)
                        for line in response.text.split("\n")[:50]:  # First 50 lines
                            logger.info(f"  {line}")
                        if response.text.count("\n") > 50:
                            logger.info(
                                f"  ... ({response.text.count(chr(10)) - 50} more lines)"
                            )
                        logger.info("=" * 60)

                    # Notify admins on auth failure (no retry — token won't heal)
                    if parsed.is_error and self._is_auth_error(
                        parsed.error_type, parsed.text
                    ):
                        self._notify_auth_failure()
                        return response

                    if not response.is_error:
                        return response
                    if attempt < self.max_retries:
                        logger.warning(f"Attempt {attempt + 1} failed, retrying...")
                        await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    logger.error(f"Claude CLI timed out after {self.timeout}s")

                    # Notify about timeout (never include prompt content)
                    await self._notify_error_with_callback(
                        error_cb,
                        "timeout",
                        {
                            "timeout_seconds": self.timeout,
                            "attempt": attempt + 1,
                        },
                    )

                    if attempt < self.max_retries:
                        await asyncio.sleep(1)
                        continue

                    return RunnerResponse(
                        text=f"Timeout: la richiesta ha impiegato più di {self.timeout} secondi. "
                        "Prova a semplificare la richiesta o riprova più tardi.",
                        session_id=session_id,
                        cost_usd=0.0,
                        is_error=True,
                        raw={},
                    )
                except Exception as e:
                    logger.exception(f"Error running Claude CLI: {e}")

                    # Notify about error
                    await self._notify_error_with_callback(
                        error_cb,
                        "exception",
                        {
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "session_id": session_id,
                            "attempt": attempt + 1,
                        },
                    )

                    return RunnerResponse(
                        text=f"Errore: {str(e)}",
                        session_id=session_id,
                        cost_usd=0.0,
                        is_error=True,
                        raw={},
                    )

            # All retries failed
            await self._notify_error_with_callback(
                error_cb,
                "retries_exhausted",
                {
                    "max_retries": self.max_retries,
                    "session_id": session_id,
                    "last_response": response.text if response else None,
                },
            )

            if response is not None:
                return response
            return RunnerResponse(
                text="Errore: tutti i tentativi falliti. Riprova più tardi.",
                session_id=session_id,
                cost_usd=0.0,
                is_error=True,
                raw={},
            )
        finally:
            # Cleanup feedback task
            if feedback_task and not feedback_task.done():
                feedback_task.cancel()
                try:
                    await feedback_task
                except asyncio.CancelledError:
                    pass

    def _build_command(
        self,
        prompt: str,
        session_id: str | None,
        mcp_config: Path | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        no_tools: bool = False,
    ) -> list[str]:
        """Build Claude CLI command.

        The prompt is passed via stdin (not as a CLI argument) to avoid
        OS ARG_MAX limits (~2MB) when the prompt grows large.
        """
        cmd = [
            "claude",
            "-p",
            "-",  # Read prompt from stdin
            "--output-format",
            "json",
            "--model",
            model or self.model,
        ]

        if no_tools:
            return cmd

        # Enable all built-in tools including WebSearch and WebFetch
        cmd.extend(["--tools", "default"])

        if mcp_config and mcp_config.exists():
            cmd.extend(["--mcp-config", str(mcp_config), "--strict-mcp-config"])

        # Add file operation permissions
        # Permissions: file ops + MCP gateway wildcard from claude_settings.json
        all_perms = self._get_file_permissions()
        if all_perms:
            cmd.extend(["--allowedTools"] + all_perms)

        if self.attachments_dir.exists():
            cmd.extend(["--add-dir", str(self.attachments_dir)])

        # Add dev project directories if they exist
        projects_dir = Path("/projects")
        if projects_dir.exists():
            for project_path in projects_dir.iterdir():
                if project_path.is_dir():
                    cmd.extend(["--add-dir", str(project_path)])

        if session_id:
            cmd.extend(["--resume", session_id])

        return cmd

    async def _run_subprocess(
        self,
        cmd: list[str],
        retry_without_resume: bool = True,
        stdin_data: str | None = None,
    ) -> str:
        """Run subprocess and return stdout.

        Args:
            cmd: Command to execute.
            retry_without_resume: Retry without --resume on session expiry.
            stdin_data: Data to pass via stdin (used for prompt).
        """
        # Inject CLAUDE_CODE_OAUTH_TOKEN the same way the pool path does,
        # otherwise the CLI authenticates as anonymous and every call fails
        # when use_pool=False (the default). See _get_cli_env for the full
        # secrets-manager + refresh flow.
        from plugins.claude.api.routes import _get_cli_env

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            env=_get_cli_env(),
            limit=4 * 1024 * 1024,  # 4MB buffer for large MCP responses
        )

        try:
            stdin_bytes = stdin_data.encode("utf-8") if stdin_data else None
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=stdin_bytes), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            logger.info("Claude subprocess killed (task cancelled)")
            raise

        stdout_str = stdout.decode()
        stderr_str = stderr.decode() if stderr else ""

        logger.debug(f"Claude stdout length: {len(stdout_str)}")
        logger.debug(f"Claude stderr: {stderr_str[:500] if stderr_str else 'empty'}")

        if not stdout_str and "No conversation found with session ID" in stderr_str:
            if retry_without_resume and "--resume" in cmd:
                logger.info("Session expired, retrying without --resume")
                new_cmd = []
                skip_next = False
                for c in cmd:
                    if skip_next:
                        skip_next = False
                        continue
                    if c == "--resume":
                        skip_next = True
                        continue
                    new_cmd.append(c)
                return await self._run_subprocess(
                    new_cmd,
                    retry_without_resume=False,
                    stdin_data=stdin_data,
                )

        if not stdout_str and stderr_str:
            logger.warning(f"No stdout, stderr: {stderr_str[:1000]}")

        return stdout_str

    async def _run_with_pool(
        self,
        agent_id: str,
        prompt: str,
        session_id: str | None,
        mcp_config_path: Path | None = None,
        tool_callback=None,
        stream_callback=None,
        model: str | None = None,
        unified_id: str | None = None,
    ) -> RunnerResponse:
        """Run request using process pool for faster response.

        Uses persistent Claude processes with stream-json protocol.

        Args:
            agent_id: Agent identifier
            prompt: The prompt text
            session_id: Optional session ID
            mcp_config_path: Path to static MCP config file for this agent
            tool_callback: Optional callback for tool use notifications
            model: Resolved model to use (already resolved by run())
            unified_id: User identity for MCP gateway side-channel
        """
        if not self._pool:
            raise RuntimeError("Process pool not initialized")

        # Get extra directories
        extra_dirs = []
        if self.attachments_dir.exists():
            extra_dirs.append(self.attachments_dir)
        projects_dir = Path("/projects")
        if projects_dir.exists():
            for project_path in projects_dir.iterdir():
                if project_path.is_dir():
                    extra_dirs.append(project_path)

        # Build tool_config from plugin config flags
        tool_config = {
            "log_calls": self.log_mcp_calls,
            "log_input": self.log_mcp_input,
            "log_output": self.log_mcp_output,
            "notify": self.notify_tool_use,
        }

        pooled = None
        try:
            # Acquire process from pool
            pooled = await self._pool.acquire(
                agent_id=agent_id,
                model=model or self.model,
                session_id=session_id,
                mcp_config_path=str(mcp_config_path) if mcp_config_path else None,
                extra_dirs=extra_dirs,
            )

            # Tell MCP gateway which user is making this request
            if unified_id:
                await self._set_user_context(agent_id, unified_id)

            # Send prompt and get response
            result = await self._pool.send_prompt(
                pooled=pooled,
                prompt=prompt,
                session_id=session_id,
                timeout=self.timeout,
                tool_callback=tool_callback,
                tool_config=tool_config,
                stream_callback=stream_callback,
            )

            # Build response
            text = result.get("text", "")
            new_session_id = result.get("session_id", session_id)
            cost = result.get("cost_usd", 0.0)
            is_error = result.get("is_error", False)
            error_type = result.get("error_type")

            # Notify admins on auth failure
            if is_error and self._is_auth_error(error_type, text):
                self._notify_auth_failure()

            if self.verbose:
                logger.info("=" * 60)
                logger.info(
                    f"[VERBOSE] POOLED RESPONSE (cost: ${cost:.4f}, error: {is_error}):"
                )
                logger.info("-" * 60)
                for line in text.split("\n")[:50]:
                    logger.info(f"  {line}")
                logger.info("=" * 60)

            return RunnerResponse(
                text=text,
                session_id=new_session_id,
                cost_usd=cost,
                is_error=is_error,
                raw=result,
            )

        except asyncio.TimeoutError:
            logger.error(f"Pooled request timed out after {self.timeout}s")
            # Destroy the process — it may be stuck mid-tool-use and
            # cannot be safely reused. A fresh process will be created
            # on the next request.
            if pooled:
                await self._pool.destroy(pooled)
                pooled = None  # prevent release in finally
            return RunnerResponse(
                text=f"Timeout: la richiesta ha impiegato più di {self.timeout} secondi.",
                session_id=session_id,
                cost_usd=0.0,
                is_error=True,
                raw={},
            )
        except asyncio.CancelledError:
            logger.info("Pooled request cancelled (user abort)")
            if pooled:
                await self._pool.destroy(pooled)
                pooled = None
            raise
        except Exception as e:
            logger.exception(f"Error in pooled request: {e}")
            return RunnerResponse(
                text=f"Errore: {str(e)}",
                session_id=session_id,
                cost_usd=0.0,
                is_error=True,
                raw={},
            )
        finally:
            if pooled:
                self._pool.release(pooled)

    async def _set_user_context(self, agent_id: str, unified_id: str) -> None:
        """Tell MCP gateway which user is making this request.

        The CLI process can't inject user_identity into JSON-RPC params,
        so we use a side-channel REST call before each prompt.
        """
        from core.mcp_token_manager import get_mcp_token_manager

        tm = get_mcp_token_manager()
        if not tm:
            return
        token = tm.get_token(agent_id)
        if not token or not tm.gateway_url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{tm.gateway_url}/mcp/user-context",
                    json={"user_identity": unified_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code != 200:
                    logger.warning("Set user context failed: HTTP %d", resp.status_code)
                else:
                    logger.debug(
                        "Set user context: agent=%s user=%s", agent_id, unified_id
                    )
        except Exception as e:
            logger.warning("Set user context failed: %s", e)
