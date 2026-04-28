"""OpenAI API Backend using the Chat Completions API.

Direct API calls via openai.AsyncOpenAI, supporting multi-turn sessions,
MCP tool calling via function calling, and streaming.
"""

import asyncio
import json
import os
import re

from config.logging_config import logger
from core.interfaces.runner import RunnerResponse
from plugins.openai.cost_tracker import calculate_cost
from plugins.openai.session_manager import SessionManager
from plugins.openai.tool_adapter import ToolAdapter
from ui.secrets_manager import secrets_manager

# Transient error substrings that warrant retry
_TRANSIENT_ERRORS = (
    "overloaded",
    "rate limit",
    "rate_limit",
    "529",
    "429",
    "503",
    "timeout",
    "connection",
    "server_error",
)

# Legacy models that still use max_tokens instead of max_completion_tokens
_LEGACY_MAX_TOKENS_MODELS = ("gpt-4o", "gpt-3.5")

# Reasoning models that require the Responses API (/v1/responses) instead of
# the Chat Completions API. Detected by name pattern; can be overridden per
# model via the registry's "endpoint" field (see _uses_responses_api).
_RESPONSES_API_PATTERNS = (
    re.compile(r"-pro$"),  # gpt-5.5-pro, gpt-5-pro, etc.
    re.compile(r"^o[1-9]"),  # o1, o3, o4-mini, ...
)


def resolve_model(name: str) -> str:
    """Map model names via registry, passthrough for unknown IDs."""
    from core.registry import get_models_registry

    registry = get_models_registry()
    if registry:
        model_map = registry.get_model_map("openai")
        if model_map:
            return model_map.get(name, name)
    return name


def _uses_legacy_max_tokens(model: str) -> bool:
    """Check if a model uses the legacy max_tokens parameter.

    Newer models (gpt-4.1, gpt-5, o-series) all use max_completion_tokens.
    Only older models (gpt-4o, gpt-3.5) still use max_tokens.
    """
    return any(model.startswith(prefix) for prefix in _LEGACY_MAX_TOKENS_MODELS)


def _uses_responses_api(model: str) -> bool:
    """Return True if the model needs the Responses API, not Chat Completions.

    Detection precedence:
      1. Registry entry has explicit ``endpoint == "responses"`` for the model
      2. Model name matches a reasoning-family pattern (`-pro` suffix or
         o-series prefix)

    Reasoning models (gpt-*-pro, o1/o3/o4) error out with HTTP 404 on
    /v1/chat/completions because they are not exposed there — they only
    answer on /v1/responses.
    """
    from core.registry import get_models_registry

    registry = get_models_registry()
    if registry:
        meta = registry.get_metadata("openai")
        if meta:
            for entry in meta.get("models", []) or []:
                if entry.get("api_id") == model or entry.get("id") == model:
                    endpoint = entry.get("endpoint")
                    if endpoint:
                        return endpoint == "responses"
                    break
    return any(p.search(model) for p in _RESPONSES_API_PATTERNS)


class OpenAIApiBackend:
    """OpenAI Chat Completions API backend.

    Follows the same structure as ClaudeApiBackend: session management,
    tool loop, streaming, retry with exponential backoff.
    """

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model", os.getenv("OPENAI_MODEL", "gpt-4.1"))
        self.timeout = config.get("timeout", 120)
        self.max_output_tokens = config.get("max_output_tokens", 8192)
        self.max_tool_iterations = config.get("max_tool_iterations", 20)
        self.max_retries = config.get("max_retries", 2)
        self.max_tools = config.get("max_tools", 0)  # 0 = unlimited

        self._client = None
        self._sessions = SessionManager(ttl_hours=config.get("session_ttl_hours", 4))
        self._tool_adapter = ToolAdapter()
        self._valid_tool_names: set[str] = set()

    async def initialize(self) -> None:
        """Initialize the OpenAI client and session cleanup."""
        import openai

        api_key = secrets_manager.get_plain("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY not set — OpenAI API backend disabled")
            return

        self._client = openai.AsyncOpenAI(api_key=api_key)
        await self._sessions.start_cleanup_loop()
        logger.info(
            "OpenAI API backend initialized (model=%s, timeout=%ds)",
            self.model,
            self.timeout,
        )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        await self._sessions.stop_cleanup_loop()
        await self._tool_adapter.shutdown()
        if self._client:
            await self._client.close()
        self._client = None
        logger.info("OpenAI API backend shut down")

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
        """Execute OpenAI API call with session, tool, and streaming support.

        Args:
            prompt: The full prompt (system + context + user message)
            session_id: Session ID for multi-turn conversations
            progress_callback: Optional async callback for progress messages
            error_callback: Optional async callback for error notifications
            tool_callback: Optional async callback for tool use notifications
            stream_callback: Optional async callback for streaming text
            agent_id: Agent identifier for MCP token and logging
            model: Model override (e.g. "gpt-4.1" or full model ID)
            no_tools: If True, skip MCP tools
            **kwargs: Additional arguments:
                system_prompt: Optional system instruction for the model

        Returns:
            RunnerResponse with text, session_id, cost, and metadata
        """
        if not self._client:
            msg = "OpenAI API client not initialized (missing OPENAI_API_KEY?)"
            logger.error(msg)
            if error_callback:
                await error_callback("runner_error", msg)
            return RunnerResponse(text=msg, is_error=True)

        effective_model = resolve_model(model or self.model)
        agent_label = agent_id or "default"

        logger.info(
            "[%s] OpenAI API call: model=%s, prompt_len=%d",
            agent_label,
            effective_model,
            len(prompt),
        )

        try:
            # --- Session management ---
            session = self._sessions.get_or_create(session_id, agent_label)

            # --- Tool setup ---
            unified_id = kwargs.get("unified_id")
            agent_max_tools = kwargs.get("max_tools")
            agent_tool_loading = kwargs.get("tool_loading", "full")
            tools = None
            if not no_tools and agent_id:
                tools = await self._setup_tools(
                    agent_id,
                    unified_id,
                    agent_max_tools,
                    tool_loading=agent_tool_loading,
                )

            # Sanitize prompt when tools are loaded to prevent text-based
            # tool mimicry
            api_prompt = self._sanitize_prompt_for_api(prompt) if tools else prompt
            messages = self._sessions.get_history(session.session_id)

            # System prompt as first message (OpenAI convention)
            system_prompt = kwargs.get("system_prompt", "")
            if tools:
                system_prompt = self._augment_system_prompt(system_prompt)

            # Build messages array: system + history + new user message
            api_messages = []
            if system_prompt:
                api_messages.append({"role": "system", "content": system_prompt})
            api_messages.extend(messages)
            api_messages.append({"role": "user", "content": api_prompt})

            # --- API call with tool loop ---
            total_cost = 0.0
            total_input_tokens = 0
            total_output_tokens = 0
            iterations = 0

            while True:
                text, tool_calls, usage_info = await self._call_api(
                    model=effective_model,
                    messages=api_messages,
                    tools=tools,
                    stream_callback=stream_callback,
                )

                # Accumulate usage
                if usage_info:
                    in_tok, out_tok, cost = usage_info
                    total_input_tokens += in_tok
                    total_output_tokens += out_tok
                    total_cost += cost

                if tool_calls and not no_tools:
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
                                "runner": "openai-api",
                                "iterations": iterations,
                                "reason": "max_tool_iterations",
                            },
                        )

                    # Add assistant message with tool_calls to conversation
                    assistant_msg = {"role": "assistant", "content": text or None}
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": (
                                    json.dumps(tc["arguments"])
                                    if isinstance(tc["arguments"], dict)
                                    else tc["arguments"]
                                ),
                            },
                        }
                        for tc in tool_calls
                    ]
                    api_messages.append(assistant_msg)

                    # Execute tool calls and add results as individual messages
                    tool_results = await self._execute_tool_calls(
                        tool_calls, tool_callback, agent_label
                    )
                    api_messages.extend(tool_results)

                    # Don't stream intermediate tool-loop iterations
                    stream_callback = None
                    continue

                # No tool calls — final text response
                break

            # Update session
            final_text = text or ""
            self._sessions.append_turn(session.session_id, "user", prompt)
            self._sessions.append_turn(session.session_id, "assistant", final_text)
            self._sessions.update_usage(
                session.session_id,
                total_input_tokens,
                total_output_tokens,
                total_cost,
            )

            logger.info(
                "[%s] OpenAI API done: in=%d out=%d cost=$%.6f iters=%d",
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
                    "runner": "openai-api",
                    "tool_iterations": iterations,
                },
            )

        except Exception as e:
            msg = f"OpenAI API error: {e}"
            logger.error("[%s] %s", agent_label, msg, exc_info=True)
            if error_callback:
                await error_callback("runner_error", str(e))
            return RunnerResponse(text=msg, is_error=True)

    # --- Internal methods ---

    async def _call_api(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream_callback=None,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Call OpenAI API with retry and optional streaming.

        Returns (text, tool_calls, usage_tuple).
        Retries on transient errors with exponential backoff.
        """
        last_error = None

        for attempt in range(1 + self.max_retries):
            if attempt > 0:
                delay = 2**attempt
                logger.info(
                    "OpenAI API retry %d/%d after %ds",
                    attempt,
                    self.max_retries,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                if stream_callback:
                    return await self._call_streaming(
                        model, messages, tools, stream_callback
                    )
                else:
                    return await self._call_unary(model, messages, tools)
            except Exception as e:
                last_error = e
                if not self._is_transient_error(e) or attempt >= self.max_retries:
                    raise
                logger.warning(
                    "Transient OpenAI API error (attempt %d): %s", attempt + 1, e
                )

        raise last_error  # pragma: no cover

    def _build_api_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict:
        """Build kwargs for chat.completions.create()."""
        kwargs = {
            "model": model,
            "messages": messages,
        }

        # Newer models use max_completion_tokens, legacy models use max_tokens
        if _uses_legacy_max_tokens(model):
            kwargs["max_tokens"] = self.max_output_tokens
        else:
            kwargs["max_completion_tokens"] = self.max_output_tokens

        if tools:
            kwargs["tools"] = tools

        return kwargs

    async def _call_unary(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Non-streaming API call. Dispatches to chat or responses backend."""
        if _uses_responses_api(model):
            return await self._call_unary_responses(model, messages, tools)
        return await self._call_unary_chat(model, messages, tools)

    async def _call_unary_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Non-streaming /v1/chat/completions call."""
        kwargs = self._build_api_kwargs(model, messages, tools)
        response = await self._client.chat.completions.create(**kwargs)

        message = response.choices[0].message
        text = message.content or ""
        tool_calls = self._extract_tool_calls(message)
        usage_info = self._extract_usage(response, model)

        return text, tool_calls, usage_info

    async def _call_unary_responses(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Non-streaming /v1/responses call for reasoning models."""
        kwargs = self._build_responses_kwargs(model, messages, tools)
        response = await self._client.responses.create(**kwargs)

        text = getattr(response, "output_text", "") or ""
        tool_calls = self._extract_responses_tool_calls(response)
        usage_info = self._extract_usage_responses(response, model)

        return text, tool_calls, usage_info

    async def _call_streaming(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        stream_callback,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Streaming API call. Dispatches to chat or responses backend."""
        if _uses_responses_api(model):
            return await self._call_streaming_responses(
                model, messages, tools, stream_callback
            )
        return await self._call_streaming_chat(model, messages, tools, stream_callback)

    async def _call_streaming_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        stream_callback,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Streaming /v1/chat/completions call."""
        kwargs = self._build_api_kwargs(model, messages, tools)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        accumulated_text = ""
        tool_calls_acc: dict[int, dict] = {}
        usage_info = None

        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                # Final chunk with usage data
                if chunk.usage:
                    in_tok = chunk.usage.prompt_tokens or 0
                    out_tok = chunk.usage.completion_tokens or 0
                    cost = calculate_cost(model, in_tok, out_tok)
                    usage_info = (in_tok, out_tok, cost)
                continue

            delta = chunk.choices[0].delta

            # Accumulate text content
            if delta.content:
                accumulated_text += delta.content
                try:
                    await stream_callback(accumulated_text)
                except Exception:
                    pass  # Never interrupt streaming

            # Accumulate tool call deltas (index-based)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = tool_calls_acc[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

        # Parse accumulated tool call arguments from JSON strings
        tool_calls = None
        if tool_calls_acc:
            tool_calls = []
            for idx in sorted(tool_calls_acc):
                tc = tool_calls_acc[idx]
                try:
                    tc["arguments"] = json.loads(tc["arguments"])
                except (json.JSONDecodeError, TypeError):
                    tc["arguments"] = {}
                tool_calls.append(tc)

        return accumulated_text, tool_calls, usage_info

    # ── Responses API helpers (reasoning models: gpt-*-pro, o-series) ────

    def _build_responses_kwargs(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
    ) -> dict:
        """Translate chat-style messages into Responses API kwargs.

        - system role → ``instructions`` parameter (concatenated)
        - user/assistant text → ``input`` items of type "message"
        - assistant tool_calls → ``input`` items of type "function_call"
        - tool role (results) → ``input`` items of type "function_call_output"
        - tools list (Chat Completions shape) → flattened to Responses shape
        """
        instructions_parts: list[str] = []
        input_items: list[dict] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content")

            if role == "system":
                if content:
                    instructions_parts.append(content)
                continue

            if role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.get("tool_call_id", ""),
                        "output": content
                        if isinstance(content, str)
                        else json.dumps(content),
                    }
                )
                continue

            if role == "assistant" and m.get("tool_calls"):
                if content:
                    input_items.append(
                        {"type": "message", "role": "assistant", "content": content}
                    )
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "")
                            if isinstance(fn.get("arguments"), str)
                            else json.dumps(fn.get("arguments") or {}),
                        }
                    )
                continue

            # Plain user/assistant message
            if content:
                input_items.append(
                    {"type": "message", "role": role, "content": content}
                )

        kwargs: dict = {
            "model": model,
            "input": input_items,
            "max_output_tokens": self.max_output_tokens,
        }
        if instructions_parts:
            kwargs["instructions"] = "\n\n".join(instructions_parts)
        if tools:
            kwargs["tools"] = self._convert_tools_for_responses(tools)
        return kwargs

    @staticmethod
    def _convert_tools_for_responses(tools: list[dict]) -> list[dict]:
        """Flatten Chat Completions tool format to Responses format.

        Chat: ``[{"type": "function", "function": {"name", "description",
                  "parameters"}}]``
        Responses: ``[{"type": "function", "name", "description",
                       "parameters"}]``
        """
        out = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                out.append(
                    {
                        "type": "function",
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                )
            else:
                out.append(t)
        return out

    @staticmethod
    def _extract_responses_tool_calls(response) -> list[dict] | None:
        """Pull function_call items out of a Responses API result."""
        output = getattr(response, "output", None) or []
        tool_calls: list[dict] = []
        for item in output:
            if getattr(item, "type", None) != "function_call":
                continue
            try:
                args_raw = getattr(item, "arguments", "") or ""
                args = json.loads(args_raw) if args_raw else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append(
                {
                    "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                    "name": getattr(item, "name", ""),
                    "arguments": args,
                }
            )
        return tool_calls or None

    @staticmethod
    def _extract_usage_responses(response, model: str) -> tuple[int, int, float] | None:
        """Read usage block from a Responses API result."""
        usage = getattr(response, "usage", None)
        if not usage:
            return None
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cost = calculate_cost(model, in_tok, out_tok)
        return (in_tok, out_tok, cost)

    async def _call_streaming_responses(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        stream_callback,
    ) -> tuple[str, list[dict] | None, tuple[int, int, float] | None]:
        """Streaming /v1/responses call.

        Listens to the typed event stream and surfaces incremental text via
        stream_callback. Tool-call deltas are accumulated by call_id.
        """
        kwargs = self._build_responses_kwargs(model, messages, tools)
        kwargs["stream"] = True

        accumulated_text = ""
        tool_calls_acc: dict[str, dict] = {}
        usage_info = None

        stream = await self._client.responses.create(**kwargs)
        async for event in stream:
            event_type = getattr(event, "type", "")

            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                if delta:
                    accumulated_text += delta
                    try:
                        await stream_callback(accumulated_text)
                    except Exception:
                        pass  # Never interrupt streaming

            elif event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if item and getattr(item, "type", "") == "function_call":
                    call_id = getattr(item, "call_id", "") or getattr(item, "id", "")
                    if call_id:
                        tool_calls_acc[call_id] = {
                            "id": call_id,
                            "name": getattr(item, "name", "") or "",
                            "arguments": getattr(item, "arguments", "") or "",
                        }

            elif event_type == "response.function_call_arguments.delta":
                call_id = getattr(event, "item_id", "") or getattr(event, "call_id", "")
                delta = getattr(event, "delta", "") or ""
                if call_id and delta and call_id in tool_calls_acc:
                    tool_calls_acc[call_id]["arguments"] += delta

            elif event_type == "response.completed":
                final = getattr(event, "response", None)
                usage_info = (
                    self._extract_usage_responses(final, model) if final else None
                )

        # Parse accumulated tool call arguments
        tool_calls: list[dict] | None = None
        if tool_calls_acc:
            tool_calls = []
            for tc in tool_calls_acc.values():
                try:
                    tc["arguments"] = (
                        json.loads(tc["arguments"]) if tc["arguments"] else {}
                    )
                except (json.JSONDecodeError, TypeError):
                    tc["arguments"] = {}
                tool_calls.append(tc)

        return accumulated_text, tool_calls, usage_info

    @staticmethod
    def _augment_system_prompt(system_prompt: str) -> str:
        """Add tool use instructions when tools are available."""
        tool_instruction = (
            "\n\n[CRITICAL - Tool Use Protocol]\n"
            "You have function calling capabilities available via the API.\n"
            "ALL services (image generation, collaboration, Odoo, HomeAssistant, "
            "Gmail, GitHub, calendars, etc.) are available as function calls. "
            "NEVER write tool names or function calls as text in your response. "
            "Use the function definitions provided to you.\n"
            "If you need to generate an image, start a collaboration, query Odoo, "
            "or use any other service, invoke the corresponding function.\n\n"
            "[CRITICAL - search_tools / execute_discovered_tool workflow]\n"
            "When you see search_tools and execute_discovered_tool functions:\n"
            "1. Call search_tools with a keyword query to find the right tool.\n"
            "2. The result contains tool entries with name, description, and "
            "inputSchema (the parameters the tool accepts).\n"
            "3. Call execute_discovered_tool with TWO fields:\n"
            '   - "tool_name": the exact tool name from the search results\n'
            '   - "arguments": an object with the parameters described in '
            "the tool's inputSchema\n"
            "IMPORTANT: You MUST read the inputSchema from the search results "
            "and fill in the arguments accordingly. Do NOT leave arguments empty.\n"
            "Example: if search_tools returns a tool named 'odoo-mcp__search' "
            "with inputSchema requiring 'model' (string), you must call:\n"
            "  execute_discovered_tool(tool_name='odoo-mcp__search', "
            "arguments={'model': 'res.partner', 'domain': []})"
        )
        return (
            (system_prompt + tool_instruction)
            if system_prompt
            else tool_instruction.strip()
        )

    @staticmethod
    def _sanitize_prompt_for_api(prompt: str) -> str:
        """Rewrite text-based MCP references for structured function calling.

        The ContextBuilder injects MCP server names that the model may copy
        into text-based calls. Rewrite these as natural language service
        descriptions so the model uses function definitions instead.
        """

        def _rewrite_mcp_permissions(match: re.Match) -> str:
            """Convert MCP Permissions block to natural language."""
            block = match.group(0)
            servers = re.findall(r"'([^']+)'", block)
            if not servers:
                if "NO access" in block:
                    return (
                        "[Permissions: This user has no access to external "
                        "services. Do not call any external service functions.]"
                    )
                return ""

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

        # Remove [Built-in Tools: ...] (covered by function definitions)
        prompt = re.sub(r"\[Built-in Tools:[^\]]*\]", "", prompt)

        # Remove inline mcp__server__tool references and "with ..." clauses
        prompt = re.sub(r"mcp__[\w-]+__\w+(?:\s+with\s+[^\n]*)?", "", prompt)

        # Rewrite "usa il tool MCP `tool_name`" personality patterns
        prompt = re.sub(
            r"usa il tool MCP `[^`]+`", "usa la funzione appropriata", prompt
        )

        # Collapse multiple blank lines left by removals
        prompt = re.sub(r"\n{3,}", "\n\n", prompt)

        return prompt

    async def _setup_tools(
        self,
        agent_id: str,
        unified_id: str | None = None,
        agent_max_tools: int | None = None,
        tool_loading: str = "full",
    ) -> list[dict] | None:
        """Initialize ToolAdapter and convert MCP tools for OpenAI."""
        try:
            await self._tool_adapter.initialize(agent_id, unified_id=unified_id)
            effective_max = agent_max_tools or self.max_tools or None
            mcp_tools = await self._tool_adapter.list_tools(
                tool_budget=effective_max,
                tool_loading=tool_loading,
            )
            if not mcp_tools:
                self._valid_tool_names = set()
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

            openai_tools = self._tool_adapter.mcp_to_openai_tools(mcp_tools)
            if not openai_tools:
                self._valid_tool_names = set()
                return None

            self._valid_tool_names = {t["function"]["name"] for t in openai_tools}
            logger.debug("[%s] Loaded %d MCP tools", agent_id, len(openai_tools))
            return openai_tools
        except Exception as e:
            logger.warning("[%s] Failed to load MCP tools: %s", agent_id, e)
            self._valid_tool_names = set()
            return None

    @staticmethod
    def _extract_tool_calls(message) -> list[dict] | None:
        """Extract tool calls from OpenAI response message."""
        if not message.tool_calls:
            return None
        calls = []
        for tc in message.tool_calls:
            try:
                arguments = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": arguments,
                }
            )
        return calls if calls else None

    @staticmethod
    def _extract_usage(response, model: str) -> tuple[int, int, float] | None:
        """Extract token usage and cost from response."""
        if not response or not hasattr(response, "usage") or not response.usage:
            return None
        usage = response.usage
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        cost = calculate_cost(model, in_tok, out_tok)
        return in_tok, out_tok, cost

    async def _execute_tool_calls(
        self,
        tool_calls: list[dict],
        tool_callback,
        agent_label: str,
    ) -> list[dict]:
        """Execute tool calls via ToolAdapter and return tool messages.

        OpenAI expects each tool result as a separate message in the array
        (unlike Claude which bundles them in a single user message).
        """
        results = []
        for tc in tool_calls:
            tool_id = tc["id"]
            name = tc["name"]
            arguments = tc["arguments"]

            logger.info("[%s] Tool call: %s", agent_label, name)

            # Reject tool names not in the provided tool definitions
            if self._valid_tool_names and name not in self._valid_tool_names:
                logger.warning(
                    "[%s] Rejected unknown tool '%s' (not in tool definitions)",
                    agent_label,
                    name,
                )
                content = [
                    {
                        "type": "text",
                        "text": (
                            f"Error: function '{name}' does not exist. "
                            "Use only functions from the provided definitions."
                        ),
                    }
                ]
                results.append(
                    self._tool_adapter.format_tool_result(
                        tool_id, content, is_error=True
                    )
                )
                continue

            if tool_callback:
                try:
                    await tool_callback(name, arguments)
                except Exception as e:
                    logger.debug("tool_callback error: %s", e)

            content = await self._tool_adapter.call_tool(name, arguments)
            results.append(self._tool_adapter.format_tool_result(tool_id, content))

        return results

    @staticmethod
    def _is_transient_error(error: Exception) -> bool:
        """Check if an error is transient and worth retrying."""
        error_str = str(error).lower()
        return any(marker in error_str for marker in _TRANSIENT_ERRORS)
