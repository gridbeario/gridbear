"""Moonshot Chat Completions backend.

Send the request; if the reply carries tool_calls, execute them through the
MCP gateway, append the results to the conversation and repeat, until the
model answers without tools.

This backend offers neither CLI-side session continuity nor token-level
streaming — Moonshot's Chat Completions API returns complete messages, not
deltas. The conversation is rebuilt on every call from the context
`MessageProcessor` supplies, plus this plugin's own SessionManager.
"""

import asyncio
import json
import os

import httpx

from config.logging_config import logger
from core.interfaces.runner import RunnerResponse

from .cost_tracker import cost_for_turn, resolve_api_id
from .credentials import MSG_NO_CREDENTIAL, read_api_key
from .session_manager import SessionManager
from .tool_adapter import ToolAdapter


def _decode_tool_arguments(raw: str | None) -> dict:
    """Decode a tool call's arguments from OpenAI's JSON-string shape.

    Moonshot mirrors OpenAI's function-calling wire format: arguments arrive
    as a JSON *string* inside ``function.arguments``, not as an object. A
    missing or empty string means no arguments. Anything that fails to
    parse, or parses to something other than an object, degrades to ``{}``
    rather than raising — one malformed tool call must not crash the whole
    turn.

    This is the only place in the plugin that needs this decode. The
    original plan shared it with a `cli_backend.TurnAccumulator`, but that
    module was dropped before this task — the CLI backend never shipped —
    so there is no second caller to share an implementation with, and no
    `ToolAdapter` static method is added for one.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as err:
        logger.warning("Kimi: could not decode tool arguments %r: %s", raw, err)
        return {}
    return decoded if isinstance(decoded, dict) else {}


class KimiApiBackend:
    """Moonshot Chat Completions, with the MCP tool loop."""

    def __init__(self, config: dict):
        self.config = config
        self.model = config.get("model", "kimi-k2.6")
        # Same three-step order KimiRunner.__init__ uses (runner.py) and
        # api/routes.py's _base_url() mirrors, so an operator's base_url
        # override reaches every path that talks to Moonshot the same way.
        self.base_url = config.get(
            "base_url", os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        )
        self.timeout = config.get("timeout", 900)
        self.max_retries = config.get("max_retries", 2)
        self.max_output_tokens = config.get("max_output_tokens", 8192)
        self.max_tool_iterations = config.get("max_tool_iterations", 20)
        self.notify_tool_use = config.get("notify_tool_use", True)
        self._sessions = SessionManager()
        self._adapter = None

    async def initialize(self) -> None:
        await self._sessions.start_cleanup_loop()

    async def shutdown(self) -> None:
        await self._sessions.stop_cleanup_loop()
        if self._adapter:
            await self._adapter.shutdown()
            self._adapter = None

    async def run(
        self,
        prompt,
        *,
        session_id=None,
        progress_callback=None,
        error_callback=None,
        tool_callback=None,
        stream_callback=None,
        agent_id=None,
        model=None,
        no_tools=False,
        **kwargs,
    ) -> RunnerResponse:
        api_key = read_api_key()
        if not api_key:
            return RunnerResponse(
                text=MSG_NO_CREDENTIAL,
                session_id=session_id,
                is_error=True,
            )

        # cost_for_turn resolves the UI id to an api_id internally (see
        # cost_tracker.py), so it must keep receiving effective_model — the
        # UI id. The wire body is a separate concern: Moonshot only ever
        # accepts the api_id it bills under, so the outgoing request uses
        # request_model instead. Resolving twice would just waste a lookup,
        # not double-convert, but there is no reason to.
        effective_model = model or self.model
        request_model = resolve_api_id(effective_model)

        session = self._sessions.get_or_create(session_id, agent_id or "default")
        history = list(self._sessions.get_history(session.session_id))
        # The user turn is NOT committed to the session here. It is included
        # in this turn's outgoing messages either way, but only written to
        # history once the turn actually succeeds below — otherwise a
        # rejected key (or any other error exit) leaves an unanswered user
        # message in history, and the next successful turn re-posts the
        # whole backlog as consecutive user messages.
        messages = history + [{"role": "user", "content": prompt}]

        tools = []
        if not no_tools and agent_id:
            tools = await self._load_tools(
                agent_id,
                kwargs.get("unified_id"),
                kwargs.get("max_tools"),
                kwargs.get("tool_loading", "full"),
            )

        total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

        for _iteration in range(self.max_tool_iterations):
            payload = await self._complete(api_key, request_model, messages, tools)
            if payload is None:
                return RunnerResponse(
                    text="The Moonshot API did not answer.",
                    session_id=session.session_id,
                    is_error=True,
                )
            if "error" in payload:
                error_info = payload["error"]
                if isinstance(error_info, dict):
                    text = str(error_info.get("message", error_info))
                    error_type = error_info.get("type")
                else:
                    text = str(error_info)
                    error_type = None
                return RunnerResponse(
                    text=text,
                    session_id=session.session_id,
                    is_error=True,
                    raw={"error_type": error_type},
                )

            usage = payload.get("usage") or {}
            for key in total_usage:
                total_usage[key] += usage.get(key, 0)

            message = payload["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                text = message.get("content") or ""
                cost = cost_for_turn(
                    effective_model,
                    total_usage["prompt_tokens"],
                    total_usage["completion_tokens"],
                )
                # Both turns committed together, and only here: this is the
                # one path through the loop that ends in an actual answer.
                self._sessions.append_turn(session.session_id, "user", prompt)
                self._sessions.append_turn(session.session_id, "assistant", text)
                self._sessions.update_usage(
                    session.session_id,
                    total_usage["prompt_tokens"],
                    total_usage["completion_tokens"],
                    cost,
                )
                if stream_callback and text:
                    # One call per complete message: Moonshot shows complete
                    # messages, so there are no deltas to forward.
                    await stream_callback(text)
                return RunnerResponse(
                    text=text,
                    session_id=session.session_id,
                    cost_usd=cost,
                    is_error=False,
                    raw={"usage": total_usage},
                )

            messages.append(message)
            for call in tool_calls:
                messages.append(await self._run_tool(call, tool_callback))

        logger.warning(
            "Kimi API tool loop hit its bound of %d iterations",
            self.max_tool_iterations,
        )
        return RunnerResponse(
            text=(
                f"The model kept calling tools past the configured bound of "
                f"{self.max_tool_iterations} iterations."
            ),
            session_id=session.session_id,
            cost_usd=cost_for_turn(
                effective_model,
                total_usage["prompt_tokens"],
                total_usage["completion_tokens"],
            ),
            is_error=True,
            raw={"usage": total_usage},
        )

    async def _load_tools(self, agent_id, unified_id, max_tools, tool_loading):
        """Load MCP tools, honouring the agent's budget and loading mode.

        `max_tools` and `tool_loading` come from the agent's YAML config,
        forwarded by main.py through runner.run()'s **kwargs. Passing them
        to list_tools lets the gateway apply both; the truncation below is
        a safety net for when it doesn't, the same convention
        plugins/openai and plugins/mistral use for their own tool budgets.
        """
        try:
            if self._adapter is None:
                self._adapter = ToolAdapter()
            await self._adapter.initialize(agent_id, unified_id)
            mcp_tools = await self._adapter.list_tools(
                tool_budget=max_tools,
                tool_loading=tool_loading,
            )
            if max_tools and len(mcp_tools) > max_tools:
                logger.warning(
                    "Kimi: runner safety net for agent %s — MCP tools (%d) "
                    "still exceed max_tools (%d) after the gateway budget",
                    agent_id,
                    len(mcp_tools),
                    max_tools,
                )
                mcp_tools = mcp_tools[:max_tools]
            return self._adapter.mcp_to_kimi_tools(mcp_tools)
        except Exception as err:  # noqa: BLE001 — logged, turn continues
            logger.warning("Kimi: could not load MCP tools: %s", err)
            return []

    async def _run_tool(self, call, tool_callback):
        function = call.get("function") or {}
        name = function.get("name", "")
        arguments = _decode_tool_arguments(function.get("arguments"))
        if tool_callback and self.notify_tool_use:
            await tool_callback(name, arguments)
        try:
            content = await self._adapter.call_tool(name, arguments)
            return self._adapter.format_tool_result(call.get("id", ""), content)
        except Exception as err:  # noqa: BLE001 — surfaced to the model
            # A failed tool is a tool result, not a runner error: the model
            # has to see it to react to it. Also logged server-side — unlike
            # _load_tools' failure, which only ever drops the tool list, an
            # MCP Gateway outage here would otherwise leave no trace but the
            # model-facing text.
            logger.warning("Kimi: tool call %s failed: %s", name, err)
            return self._adapter.format_tool_result(
                call.get("id", ""), [{"type": "text", "text": str(err)}], True
            )

    async def _complete(self, api_key, model, messages, tools):
        """POST one completion, with the repo's retry convention.

        max_retries = 2 means two retries, three attempts in total —
        `range(1 + self.max_retries)`, the convention already used by
        `plugins/mistral/api_backend.py` and `plugins/openai/api_backend.py`.
        """
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
        }
        if tools:
            body["tools"] = tools

        last = None
        for attempt in range(1 + self.max_retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url.rstrip('/')}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                payload = response.json()
                if response.status_code == 200:
                    return payload
                if response.status_code in (401, 403):
                    # Observable as a status code here, unlike on the CLI
                    # backend where this would be buried in a subprocess.
                    # Not retried: a rejected credential will not heal in
                    # 200ms.
                    return payload
                logger.warning(
                    "Kimi API attempt %d/%d: HTTP %d",
                    attempt + 1,
                    1 + self.max_retries,
                    response.status_code,
                )
                last = payload
            except Exception as err:  # noqa: BLE001 — retried, then reported
                logger.warning("Kimi API attempt %d failed: %s", attempt + 1, err)
                last = {"error": {"message": str(err)}}
            if attempt < self.max_retries:
                await asyncio.sleep(2**attempt)
        return last
