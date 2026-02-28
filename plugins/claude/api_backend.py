"""Claude API Backend using the Anthropic SDK.

Direct API calls via anthropic.AsyncAnthropic, bypassing the CLI subprocess.
Supports multi-turn sessions, MCP tool calling, and streaming.
"""

import asyncio
import os
import re

from config.logging_config import logger
from core.interfaces.runner import RunnerResponse
from plugins.claude.cost_tracker import calculate_cost
from plugins.claude.session_manager import SessionManager
from plugins.claude.tool_adapter import ToolAdapter
from ui.secrets_manager import secrets_manager

# Fallback map when registry is unavailable
_DEFAULT_MODEL_MAP = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6-20250827",
}

# Transient error substrings that warrant retry
_TRANSIENT_ERRORS = (
    "overloaded",
    "rate limit",
    "529",
    "429",
    "503",
    "timeout",
    "connection",
)


def resolve_model(name: str) -> str:
    """Map short model names to full API IDs, passthrough for full IDs."""
    from core.registry import get_models_registry

    registry = get_models_registry()
    if registry:
        model_map = registry.get_model_map("claude")
        if model_map:
            return model_map.get(name, name)
    return _DEFAULT_MODEL_MAP.get(name, name)


class ClaudeApiBackend:
    """Claude API backend using anthropic.AsyncAnthropic.

    Follows the same structure as GeminiRunner: session management,
    tool loop, streaming, retry with exponential backoff.
    """

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model", os.getenv("CLAUDE_MODEL", "sonnet"))
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
        """Initialize the Anthropic client and session cleanup."""
        import anthropic

        api_key = secrets_manager.get_plain("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not set — Claude API backend disabled")
            return

        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        await self._sessions.start_cleanup_loop()
        logger.info(
            "Claude API backend initialized (model=%s, timeout=%ds)",
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
        logger.info("Claude API backend shut down")

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
        """Execute Claude API call with session, tool, and streaming support.

        Args:
            prompt: The full prompt (system + context + user message)
            session_id: Session ID for multi-turn conversations
            progress_callback: Optional async callback for progress messages
            error_callback: Optional async callback for error notifications
            tool_callback: Optional async callback for tool use notifications
            stream_callback: Optional async callback for streaming text
            agent_id: Agent identifier for MCP token and logging
            use_pool: Ignored (no process pool for API backend)
            model: Model override (e.g. "opus" or full model ID)
            no_tools: If True, skip MCP tools
            **kwargs: Additional arguments:
                system_prompt: Optional system instruction for the model

        Returns:
            RunnerResponse with text, session_id, cost, and metadata
        """
        if not self._client:
            msg = "Claude API client not initialized (missing ANTHROPIC_API_KEY?)"
            logger.error(msg)
            if error_callback:
                await error_callback("runner_error", msg)
            return RunnerResponse(text=msg, is_error=True)

        effective_model = resolve_model(model or self.model)
        agent_label = agent_id or "default"

        logger.info(
            "[%s] Claude API call: model=%s, prompt_len=%d",
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

            # Build messages: session history + new user message
            # When tools are loaded, sanitize prompt to remove text-based
            # tool references that cause the model to mimic them as text
            # instead of using structured tool_use.
            api_prompt = self._sanitize_prompt_for_api(prompt) if tools else prompt
            messages = self._sessions.get_history(session.session_id)
            messages.append({"role": "user", "content": api_prompt})

            # System prompt (native API parameter, not embedded)
            system_prompt = kwargs.get("system_prompt", "")

            # When tools are available, add function calling instruction
            if tools:
                system_prompt = self._augment_system_prompt(system_prompt)

            # --- API call with tool loop ---
            total_cost = 0.0
            total_input_tokens = 0
            total_output_tokens = 0
            iterations = 0

            while True:
                text, response = await self._call_api(
                    model=effective_model,
                    messages=messages,
                    system=system_prompt,
                    tools=tools,
                    stream_callback=stream_callback,
                )

                # Accumulate usage
                usage_data = self._extract_usage(response, effective_model)
                if usage_data:
                    in_tok, out_tok, cost = usage_data
                    total_input_tokens += in_tok
                    total_output_tokens += out_tok
                    total_cost += cost

                # Check for tool_use blocks
                tool_use_blocks = self._extract_tool_use(response)

                if tool_use_blocks and not no_tools:
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
                                "runner": "claude-api",
                                "iterations": iterations,
                                "reason": "max_tool_iterations",
                            },
                        )

                    # Add assistant's full response (with tool_use) to messages
                    messages.append(self._format_assistant_message(response))

                    # Execute tool calls and add results
                    tool_results = await self._execute_tool_calls(
                        tool_use_blocks, tool_callback, agent_label
                    )
                    messages.append({"role": "user", "content": tool_results})

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
                "[%s] Claude API done: in=%d out=%d cost=$%.6f iters=%d",
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
                    "runner": "claude-api",
                    "tool_iterations": iterations,
                },
            )

        except Exception as e:
            msg = f"Claude API error: {e}"
            logger.error("[%s] %s", agent_label, msg, exc_info=True)
            if error_callback:
                await error_callback("runner_error", str(e))
            return RunnerResponse(text=msg, is_error=True)

    # --- Internal methods ---

    async def _call_api(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        stream_callback=None,
    ) -> tuple[str, object]:
        """Call Claude API with retry and optional streaming.

        Returns (text, response_message).
        Retries on transient errors with exponential backoff.
        """
        for attempt in range(1 + self.max_retries):
            if attempt > 0:
                delay = 2**attempt
                logger.info(
                    "Claude API retry %d/%d after %ds",
                    attempt,
                    self.max_retries,
                    delay,
                )
                await asyncio.sleep(delay)

            try:
                if stream_callback:
                    return await self._call_streaming(
                        model, messages, system, tools, stream_callback
                    )
                else:
                    return await self._call_unary(model, messages, system, tools)
            except Exception as e:
                if not self._is_transient_error(e) or attempt >= self.max_retries:
                    raise
                logger.warning(
                    "Transient Claude API error (attempt %d): %s", attempt + 1, e
                )

        raise RuntimeError("max retries exceeded")  # pragma: no cover

    async def _call_unary(
        self,
        model: str,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
    ) -> tuple[str, object]:
        """Non-streaming API call."""
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = await self._client.messages.create(**kwargs)
        text = self._extract_text(response)
        return text, response

    async def _call_streaming(
        self,
        model: str,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
        stream_callback,
    ) -> tuple[str, object]:
        """Streaming API call — calls stream_callback with accumulated text."""
        kwargs = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        accumulated_text = ""
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                accumulated_text += text
                try:
                    await stream_callback(accumulated_text)
                except Exception:
                    pass  # Never interrupt streaming

            response = await stream.get_final_message()

        return accumulated_text, response

    @staticmethod
    def _augment_system_prompt(system_prompt: str) -> str:
        """Add tool use instructions when tools are available."""
        tool_instruction = (
            "\n\n[CRITICAL - Tool Use Protocol]\n"
            "You have structured tool_use capabilities available via the API.\n"
            "ALL services (image generation, collaboration, Odoo, HomeAssistant, "
            "Gmail, GitHub, calendars, etc.) are available as structured tool "
            "calls. NEVER write tool names or function calls as text in your "
            "response. Use the tool definitions provided to you.\n"
            "If you need to generate an image, start a collaboration, query Odoo, "
            "or use any other service, invoke the corresponding tool."
        )
        return (
            (system_prompt + tool_instruction)
            if system_prompt
            else tool_instruction.strip()
        )

    @staticmethod
    def _sanitize_prompt_for_api(prompt: str) -> str:
        """Rewrite text-based MCP references for structured tool_use.

        The ContextBuilder injects MCP server names (e.g. 'odoo-mcp') that
        the model may copy into text-based calls. Rewrite these as natural
        language service descriptions so the model uses the tool definitions
        instead.
        """

        def _rewrite_mcp_permissions(match: re.Match) -> str:
            """Convert MCP Permissions block to natural language."""
            block = match.group(0)
            servers = re.findall(r"'([^']+)'", block)
            if not servers:
                if "NO access" in block:
                    return (
                        "[Permissions: This user has no access to external "
                        "services. Do not call any external service tools.]"
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
                f"Use the corresponding tool_use calls for these services. "
                f"Do not call tools for services not listed.]"
            )

        # Rewrite [MCP Permissions: ...] with natural language
        prompt = re.sub(r"\[MCP Permissions:[^\]]*\]", _rewrite_mcp_permissions, prompt)

        # Remove [Built-in Tools: ...] (covered by tool definitions)
        prompt = re.sub(r"\[Built-in Tools:[^\]]*\]", "", prompt)

        # Remove inline mcp__server__tool references and "with ..." clauses
        prompt = re.sub(r"mcp__[\w-]+__\w+(?:\s+with\s+[^\n]*)?", "", prompt)

        # Rewrite "usa il tool MCP `tool_name`" personality patterns
        prompt = re.sub(
            r"usa il tool MCP `[^`]+`", "usa il tool_use appropriato", prompt
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
        """Initialize ToolAdapter and convert MCP tools for Claude."""
        try:
            await self._tool_adapter.initialize(agent_id, unified_id=unified_id)
            # Agent YAML max_tools overrides runner config
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

            claude_tools = self._tool_adapter.mcp_to_claude_tools(mcp_tools)
            if not claude_tools:
                self._valid_tool_names = set()
                return None

            self._valid_tool_names = {t["name"] for t in claude_tools}
            logger.debug("[%s] Loaded %d MCP tools", agent_id, len(claude_tools))
            return claude_tools
        except Exception as e:
            logger.warning("[%s] Failed to load MCP tools: %s", agent_id, e)
            self._valid_tool_names = set()
            return None

    @staticmethod
    def _extract_text(response) -> str:
        """Extract text from Claude response content blocks."""
        parts = []
        for block in response.content:
            if block.type == "text":
                parts.append(block.text)
        return "\n".join(parts) if parts else ""

    @staticmethod
    def _extract_tool_use(response) -> list[dict]:
        """Extract tool_use blocks from Claude response."""
        blocks = []
        for block in response.content:
            if block.type == "tool_use":
                blocks.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return blocks

    @staticmethod
    def _extract_usage(response, model: str) -> tuple[int, int, float] | None:
        """Extract token usage and cost from response."""
        if not response or not hasattr(response, "usage") or not response.usage:
            return None
        usage = response.usage
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        cost = calculate_cost(model, in_tok, out_tok)
        return in_tok, out_tok, cost

    @staticmethod
    def _format_assistant_message(response) -> dict:
        """Format Claude's response as an assistant message for the messages array.

        Preserves both text and tool_use blocks so the tool loop context
        is maintained correctly.
        """
        content = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
        return {"role": "assistant", "content": content}

    async def _execute_tool_calls(
        self,
        tool_use_blocks: list[dict],
        tool_callback,
        agent_label: str,
    ) -> list[dict]:
        """Execute tool calls via ToolAdapter and return tool_result blocks."""
        results = []
        for block in tool_use_blocks:
            tool_id = block["id"]
            name = block["name"]
            arguments = block["input"]

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
                            f"Error: tool '{name}' does not exist. "
                            "Use only tools from the provided tool definitions."
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
