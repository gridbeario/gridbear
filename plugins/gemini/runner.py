"""Gemini Runner Plugin.

Executes Google Gemini via the google-genai SDK.
Supports multi-turn sessions, MCP tool calling, and streaming.
"""

import asyncio
import os
import re

from config.logging_config import logger
from core.interfaces.runner import BaseRunner, RunnerResponse
from plugins.gemini.cost_tracker import calculate_cost
from plugins.gemini.session_manager import SessionManager
from plugins.gemini.tool_adapter import ToolAdapter
from ui.secrets_manager import secrets_manager

# Transient error substrings that warrant retry
_TRANSIENT_ERRORS = (
    "resource exhausted",
    "rate limit",
    "quota exceeded",
    "deadline exceeded",
    "timeout",
    "internal error",
    "503",
    "429",
)


class GeminiRunner(BaseRunner):
    """Google Gemini AI runner using the genai SDK."""

    name = "gemini"

    def __init__(self, config: dict):
        super().__init__(config)
        self.model = config.get("model", os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
        self.timeout = config.get("timeout", 120)
        self.temperature = config.get("temperature", 0.7)
        self.max_output_tokens = config.get("max_output_tokens", 8192)
        self.max_tool_iterations = config.get("max_tool_iterations", 20)
        self.max_retries = config.get("max_retries", 2)
        self.max_tools = config.get("max_tools", 0)  # 0 = unlimited

        self._client = None
        self._sessions = SessionManager(ttl_hours=config.get("session_ttl_hours", 4))
        self._tool_adapter = ToolAdapter()

    async def initialize(self) -> None:
        """Initialize the Gemini client and session cleanup."""
        from google import genai

        api_key = secrets_manager.get_plain("GOOGLE_AI_API_KEY")
        if not api_key:
            logger.error("GOOGLE_AI_API_KEY not set — Gemini runner disabled")
            return

        self._client = genai.Client(api_key=api_key)
        await self._sessions.start_cleanup_loop()
        logger.info(
            "Gemini runner initialized (model=%s, timeout=%ds)",
            self.model,
            self.timeout,
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        await self._sessions.stop_cleanup_loop()
        await self._tool_adapter.shutdown()
        self._client = None
        logger.info("Gemini runner shut down")

    async def supports_tools(self) -> bool:
        """Gemini supports tools via MCP Gateway."""
        return True

    async def supports_vision(self) -> bool:
        """Gemini supports image input."""
        return True

    _DEFAULT_MODELS = [
        {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        {"id": "gemini-2.0-flash-lite", "name": "Gemini 2.0 Flash Lite"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
    ]

    @property
    def available_models(self) -> list[tuple[str, str]]:
        """Return Gemini model choices from registry."""
        from core.registry import get_models_registry

        registry = get_models_registry()
        if registry:
            registry.seed_if_empty("gemini", self._DEFAULT_MODELS)
            models = registry.get_for_ui("gemini")
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
        use_pool: bool | None = None,
        model: str | None = None,
        no_tools: bool = False,
        **kwargs,
    ) -> RunnerResponse:
        """Execute Gemini API call with session, tool, and streaming support.

        Args:
            prompt: The full prompt (system + context + user message)
            session_id: Session ID for multi-turn conversations
            progress_callback: Optional async callback for progress messages
            error_callback: Optional async callback for error notifications
            tool_callback: Optional async callback for tool use notifications
            stream_callback: Optional async callback for streaming text
            agent_id: Agent identifier for MCP token and logging
            use_pool: Ignored (no process pool for API-based runner)
            model: Model override (e.g. "gemini-2.5-pro")
            no_tools: If True, skip MCP tools
            **kwargs: Additional arguments:
                system_prompt: Optional system instruction for the model

        Returns:
            RunnerResponse with text, session_id, cost, and metadata
        """
        if not self._client:
            msg = "Gemini client not initialized (missing GOOGLE_AI_API_KEY?)"
            logger.error(msg)
            if error_callback:
                await error_callback("runner_error", msg)
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
                        ("gemini",),
                    ).fetchone()
                if row and row["config"]:
                    effective_model = row["config"].get("model", self.model)
            except Exception:
                pass
            if not effective_model:
                effective_model = self.model

        agent_label = agent_id or "default"

        logger.info(
            "[%s] Gemini call: model=%s, prompt_len=%d",
            agent_label,
            effective_model,
            len(prompt),
        )

        try:
            from google.genai import types

            # --- Session management ---
            session = self._sessions.get_or_create(session_id, agent_label)

            # --- Tool setup ---
            unified_id = kwargs.get("unified_id")
            agent_max_tools = kwargs.get("max_tools")
            agent_tool_loading = kwargs.get("tool_loading", "full")
            tools_config = None
            if not no_tools and agent_id:
                tools_config = await self._setup_tools(
                    agent_id,
                    types,
                    unified_id,
                    agent_max_tools,
                    tool_loading=agent_tool_loading,
                )

            # Build generation config with optional system instruction
            system_prompt = kwargs.get("system_prompt", "")

            # When tools are available, add function calling instruction
            if tools_config:
                system_prompt = self._augment_system_prompt(system_prompt)

            gen_kwargs = {
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
            }
            if system_prompt:
                gen_kwargs["system_instruction"] = system_prompt
            if tools_config:
                gen_kwargs["tools"] = tools_config
                # Explicit AUTO mode: model decides when to call functions
                # vs respond with text — reinforces structured calling
                gen_kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="AUTO",
                    )
                )
            gen_config = types.GenerateContentConfig(**gen_kwargs)

            # Build contents: session history + new user message
            # When tools are loaded, sanitize prompt to remove text-based
            # tool references that cause the model to mimic them as text
            # instead of using structured function calling.
            api_prompt = (
                self._sanitize_prompt_for_api(prompt) if tools_config else prompt
            )
            contents = self._sessions.get_history(session.session_id)
            contents.append({"role": "user", "parts": [{"text": api_prompt}]})

            # --- API call with tool loop ---
            total_cost = 0.0
            total_input_tokens = 0
            total_output_tokens = 0
            iterations = 0

            while True:
                # Use streaming for the generation call when callback provided
                text, response, function_calls = await self._generate(
                    effective_model,
                    contents,
                    gen_config,
                    stream_callback,
                )

                # Check for safety blocks
                if response and self._is_safety_blocked(response):
                    block_reason = str(response.prompt_feedback.block_reason)
                    msg = f"Request blocked by safety filter: {block_reason}"
                    logger.warning("[%s] %s", agent_label, msg)
                    if error_callback:
                        await error_callback("safety_block", msg)
                    return RunnerResponse(text=msg, is_error=True)

                # Accumulate usage
                cost_usd = self._extract_usage(response, effective_model)
                if cost_usd:
                    in_tok, out_tok, cost = cost_usd
                    total_input_tokens += in_tok
                    total_output_tokens += out_tok
                    total_cost += cost

                # Handle function calls
                if function_calls and not no_tools:
                    iterations += 1
                    if iterations > self.max_tool_iterations:
                        msg = (
                            f"Max tool iterations ({self.max_tool_iterations}) reached. "
                            "Request interrupted."
                        )
                        logger.warning("[%s] %s", agent_label, msg)
                        return RunnerResponse(
                            text=msg,
                            session_id=session.session_id,
                            cost_usd=total_cost,
                            is_error=True,
                            raw={
                                "model": effective_model,
                                "runner": "gemini",
                                "iterations": iterations,
                                "reason": "max_tool_iterations",
                            },
                        )

                    # Add the model's function call response to contents
                    contents.append(self._format_model_response(response))

                    # Execute tool calls
                    tool_results = await self._execute_function_calls(
                        function_calls, tool_callback, agent_label
                    )
                    for result in tool_results:
                        contents.append(result)

                    # Don't stream intermediate tool-loop iterations
                    stream_callback = None
                    continue

                # No function calls — final text response
                break

            # Update session
            final_text = text or ""
            self._sessions.append_turn(session.session_id, "user", prompt)
            self._sessions.append_turn(session.session_id, "model", final_text)
            self._sessions.update_usage(
                session.session_id,
                total_input_tokens,
                total_output_tokens,
                total_cost,
            )

            logger.info(
                "[%s] Gemini done: in=%d out=%d cost=$%.6f iters=%d",
                agent_label,
                total_input_tokens,
                total_output_tokens,
                total_cost,
                iterations,
            )

            return RunnerResponse(
                text=final_text,
                session_id=session.session_id,
                cost_usd=total_cost,
                raw={
                    "model": effective_model,
                    "runner": "gemini",
                    "tool_iterations": iterations,
                },
            )

        except Exception as e:
            msg = f"Gemini API error: {e}"
            logger.error("[%s] %s", agent_label, msg, exc_info=True)
            if error_callback:
                await error_callback("runner_error", str(e))
            return RunnerResponse(text=msg, is_error=True)

    # --- Internal methods ---

    async def _generate(
        self,
        model: str,
        contents: list,
        config,
        stream_callback=None,
    ) -> tuple[str, object, list[dict]]:
        """Call Gemini API with retry and optional streaming.

        Returns (text, response_object, function_calls).
        Uses streaming when stream_callback is provided.
        Retries on transient errors with exponential backoff.
        """
        last_error = None

        for attempt in range(1 + self.max_retries):
            if attempt > 0:
                delay = 2**attempt
                logger.info(
                    "Gemini retry %d/%d after %ds", attempt, self.max_retries, delay
                )
                await asyncio.sleep(delay)

            try:
                if stream_callback:
                    return await self._generate_streaming(
                        model, contents, config, stream_callback
                    )
                else:
                    return await self._generate_unary(model, contents, config)
            except Exception as e:
                last_error = e
                if not self._is_transient_error(e) or attempt >= self.max_retries:
                    raise
                logger.warning(
                    "Transient Gemini error (attempt %d): %s", attempt + 1, e
                )

        raise last_error  # pragma: no cover

    async def _generate_unary(
        self,
        model: str,
        contents: list,
        config,
    ) -> tuple[str, object, list[dict]]:
        """Non-streaming generation call."""
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        text = response.text or ""
        function_calls = self._extract_function_calls(response)
        return text, response, function_calls

    async def _generate_streaming(
        self,
        model: str,
        contents: list,
        config,
        stream_callback,
    ) -> tuple[str, object, list[dict]]:
        """Streaming generation — calls stream_callback with accumulated text."""
        accumulated_text = ""
        last_response = None
        function_calls = []

        async for chunk in self._client.aio.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        ):
            last_response = chunk

            # Collect function calls from streaming chunks
            chunk_fcs = self._extract_function_calls(chunk)
            if chunk_fcs:
                function_calls.extend(chunk_fcs)
                continue

            # Accumulate text and stream to callback
            chunk_text = getattr(chunk, "text", None) or ""
            if chunk_text:
                accumulated_text += chunk_text
                try:
                    await stream_callback(accumulated_text)
                except Exception:
                    pass  # Never interrupt streaming

        return accumulated_text, last_response, function_calls

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        """Check if an error is transient and worth retrying."""
        error_str = str(error).lower()
        return any(marker in error_str for marker in _TRANSIENT_ERRORS)

    @staticmethod
    def _is_safety_blocked(response) -> bool:
        """Check if response was blocked by safety filters."""
        return (
            hasattr(response, "prompt_feedback")
            and response.prompt_feedback
            and hasattr(response.prompt_feedback, "block_reason")
            and response.prompt_feedback.block_reason
        )

    @staticmethod
    def _extract_usage(response, model: str) -> tuple[int, int, float] | None:
        """Extract token usage and cost from response metadata."""
        if (
            not response
            or not hasattr(response, "usage_metadata")
            or not response.usage_metadata
        ):
            return None
        usage = response.usage_metadata
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        cost = calculate_cost(model, in_tok, out_tok)
        return in_tok, out_tok, cost

    @staticmethod
    def _augment_system_prompt(system_prompt: str) -> str:
        """Add function calling instructions when tools are available."""
        tool_instruction = (
            "\n\n[CRITICAL - Function Calling Protocol]\n"
            "You have structured function calling tools available via the API.\n"
            "ALL services (image generation, collaboration, Odoo, HomeAssistant, "
            "Gmail, GitHub, calendars, etc.) are available as structured function "
            "calls. NEVER write tool names or function calls as text in your "
            "response. Use the function declarations provided to you.\n"
            "If you need to generate an image, start a collaboration, query Odoo, "
            "or use any other service, invoke the corresponding function."
        )
        return (
            (system_prompt + tool_instruction)
            if system_prompt
            else tool_instruction.strip()
        )

    @staticmethod
    def _sanitize_prompt_for_api(prompt: str) -> str:
        """Rewrite text-based MCP references for structured function calling.

        The ContextBuilder injects MCP server names (e.g. 'odoo-mcp') that
        the model copies into text-based calls. Rewrite these as natural
        language service descriptions so the model uses the function
        declarations instead.
        """

        def _rewrite_mcp_permissions(match: re.Match) -> str:
            """Convert MCP Permissions block to natural language."""
            block = match.group(0)
            # Extract server names from 'server-name' patterns
            servers = re.findall(r"'([^']+)'", block)
            if not servers:
                if "NO access" in block:
                    return (
                        "[Permissions: This user has no access to external "
                        "services. Do not call any external service functions.]"
                    )
                return ""

            # Convert server names to readable service names
            readable = []
            for s in servers:
                if s.startswith("gmail-"):
                    readable.append(f"Gmail ({s[6:]})")
                elif s.startswith("gws-"):
                    readable.append(f"Google Workspace ({s[4:]})")
                elif s.startswith("ms365-"):
                    readable.append(f"Microsoft 365 ({s[6:]})")
                elif s == "odoo-mcp":
                    readable.append("Odoo")
                elif s == "homeassistant":
                    readable.append("HomeAssistant")
                elif s == "playwright":
                    readable.append("Web Browser (Playwright)")
                elif s == "github":
                    readable.append("GitHub")
                else:
                    # Generic: remove -mcp suffix, capitalize
                    clean = s.replace("-mcp", "").replace("-", " ").title()
                    readable.append(clean)

            services = ", ".join(readable)
            return (
                f"[Available Services: This user can access: {services}. "
                f"Use the corresponding function calls for these services. "
                f"Do not call functions for services not listed.]"
            )

        # Rewrite [MCP Permissions: ...] with natural language
        prompt = re.sub(r"\[MCP Permissions:[^\]]*\]", _rewrite_mcp_permissions, prompt)

        # Remove [Built-in Tools: ...] (covered by function declarations)
        prompt = re.sub(r"\[Built-in Tools:[^\]]*\]", "", prompt)

        # Remove inline mcp__server__tool references and "with ..." clauses
        prompt = re.sub(r"mcp__[\w-]+__\w+(?:\s+with\s+[^\n]*)?", "", prompt)

        # Rewrite "usa il tool MCP `tool_name`" personality patterns
        prompt = re.sub(
            r"usa il tool MCP `[^`]+`", "usa il function call appropriato", prompt
        )

        # Collapse multiple blank lines left by removals
        prompt = re.sub(r"\n{3,}", "\n\n", prompt)

        return prompt

    async def _setup_tools(
        self,
        agent_id: str,
        types_module,
        unified_id: str | None = None,
        agent_max_tools: int | None = None,
        tool_loading: str = "full",
    ) -> list | None:
        """Initialize ToolAdapter and convert MCP tools for Gemini."""
        try:
            await self._tool_adapter.initialize(agent_id, unified_id=unified_id)
            # Agent YAML max_tools overrides runner config
            effective_max = agent_max_tools or self.max_tools or None
            mcp_tools = await self._tool_adapter.list_tools(
                tool_budget=effective_max,
                tool_loading=tool_loading,
            )
            if not mcp_tools:
                logger.info("[%s] No MCP tools available", agent_id)
                return None

            # Safety net: gateway should have already applied the budget
            if effective_max and len(mcp_tools) > effective_max:
                logger.warning(
                    "[%s] Runner safety net: MCP tools (%d) still exceed "
                    "max_tools (%d) after gateway budget",
                    agent_id,
                    len(mcp_tools),
                    effective_max,
                )
                mcp_tools = mcp_tools[:effective_max]

            declarations = self._tool_adapter.mcp_to_gemini_declarations(mcp_tools)
            if not declarations:
                return None

            tools = [types_module.Tool(function_declarations=declarations)]
            logger.info(
                "[%s] Loaded %d MCP tools for Gemini", agent_id, len(declarations)
            )
            return tools
        except Exception as e:
            logger.warning("[%s] Failed to load MCP tools: %s", agent_id, e)
            return None

    @staticmethod
    def _extract_function_calls(response) -> list[dict]:
        """Extract function calls from Gemini response candidates."""
        calls = []
        try:
            if not hasattr(response, "candidates") or not response.candidates:
                return []
            for candidate in response.candidates:
                if not hasattr(candidate, "content") or not candidate.content:
                    continue
                for part in candidate.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        calls.append(
                            {
                                "name": fc.name,
                                "args": dict(fc.args) if fc.args else {},
                            }
                        )
        except Exception as e:
            logger.debug("Error extracting function calls: %s", e)
        return calls

    @staticmethod
    def _format_model_response(response) -> dict:
        """Format the model's response (with function calls) as a content dict."""
        parts = []
        try:
            for candidate in response.candidates:
                if not hasattr(candidate, "content") or not candidate.content:
                    continue
                for part in candidate.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        parts.append(
                            {
                                "function_call": {
                                    "name": fc.name,
                                    "args": dict(fc.args) if fc.args else {},
                                }
                            }
                        )
                    elif hasattr(part, "text") and part.text:
                        parts.append({"text": part.text})
        except Exception as e:
            logger.debug("Error formatting model response: %s", e)
        return {"role": "model", "parts": parts}

    async def _execute_function_calls(
        self,
        function_calls: list[dict],
        tool_callback,
        agent_label: str,
    ) -> list[dict]:
        """Execute function calls via ToolAdapter and return results."""
        # Build set of declared tool names for validation
        declared = set()
        if self._tool_adapter._tools_cache:
            for t in self._tool_adapter._tools_cache:
                declared.add(ToolAdapter._sanitize_name(t.get("name", "")))

        results = []
        for fc in function_calls:
            name = fc["name"]
            args = fc["args"]

            # Reject hallucinated tool names not in declarations
            if declared and name not in declared:
                logger.warning(
                    "[%s] Rejected undeclared tool call: %s", agent_label, name
                )
                results.append(
                    self._tool_adapter.format_tool_result(
                        name,
                        [
                            {
                                "type": "text",
                                "text": f"Error: tool '{name}' is not directly available. "
                                "You must use search_tools to find it first, then "
                                "execute_discovered_tool to call it. Example:\n"
                                "1. Call search_tools with query matching the tool "
                                f"(e.g. '{name.split('__')[-1] if '__' in name else name}')\n"
                                "2. Call execute_discovered_tool with the exact tool_name "
                                "from the search results and the arguments.",
                            }
                        ],
                    )
                )
                continue

            logger.info("[%s] Tool call: %s", agent_label, name)

            if tool_callback:
                try:
                    await tool_callback(name, args)
                except Exception as e:
                    logger.debug("tool_callback error: %s", e)

            content = await self._tool_adapter.call_tool(name, args)
            results.append(self._tool_adapter.format_tool_result(name, content))

        return results
