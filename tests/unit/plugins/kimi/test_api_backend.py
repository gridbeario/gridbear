"""The tool-calling loop, with httpx mocked.

A 401 here IS observable as response.status_code, unlike on the CLI backend
where the HTTP call happens inside a subprocess — which is why the two paths
recognise authentication failures differently.
"""

import pytest


@pytest.fixture
def vault(monkeypatch):
    store = {"MOONSHOT_API_KEY": "sk-test-key"}

    class _Vault:
        def get_plain(self, key):
            return store.get(key)

        def set(self, key, value):
            store[key] = value

        def delete(self, key):
            store.pop(key, None)

    monkeypatch.setattr("ui.secrets_manager.secrets_manager", _Vault())
    return store


def _completion(content=None, tool_calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def transport(monkeypatch):
    """Queue completions; record every request body."""
    from plugins.kimi import api_backend

    queue = []
    requests = []

    class _Response:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

    async def _post(self, url, **kwargs):
        requests.append(kwargs.get("json"))
        payload, status = queue.pop(0)
        return _Response(payload, status)

    monkeypatch.setattr(api_backend.httpx.AsyncClient, "post", _post)
    transport.queue = queue
    transport.requests = requests
    return transport


class TestPlainCompletion:
    @pytest.mark.asyncio
    async def test_a_reply_without_tools_ends_the_loop(self, vault, transport):
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append((_completion(content="hello"), 200))
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("hi")
        assert response.text == "hello"
        assert response.is_error is False
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_the_turn_is_costed_from_usage_through_the_api_id(
        self, vault, transport, monkeypatch
    ):
        from plugins.kimi import cost_tracker
        from plugins.kimi.api_backend import KimiApiBackend

        # "kimi-k2-turbo" here is a deliberately fictional id, not the real
        # catalogue: this test pins the costing *mechanism* (config model ->
        # monkeypatched price table -> cost_usd), not the real rate table,
        # so it stays valid regardless of what Moonshot's actual catalogue
        # holds. Not the same string as runner.py's model default.
        monkeypatch.setattr(cost_tracker, "KIMI_PRICING", [("kimi-k2-turbo", 1.0, 2.0)])
        transport.queue.append(
            (
                _completion(
                    content="ok",
                    usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
                ),
                200,
            )
        )
        backend = KimiApiBackend({"model": "kimi-k2-turbo"})
        await backend.initialize()
        response = await backend.run("hi")
        assert response.cost_usd == pytest.approx(1.0)


class TestToolLoop:
    @pytest.mark.asyncio
    async def test_a_tool_call_is_executed_and_the_loop_continues(
        self, vault, transport, monkeypatch
    ):
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append(
            (
                _completion(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "t", "arguments": "{}"},
                        }
                    ]
                ),
                200,
            )
        )
        transport.queue.append((_completion(content="done"), 200))

        executed = []

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, *a, **k):
                return [{"name": "t", "description": "", "inputSchema": {}}]

            async def call_tool(self, name, arguments):
                executed.append(name)
                return [{"type": "text", "text": "tool output"}]

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            def format_tool_result(self, tool_call_id, content, is_error=False):
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content[0]["text"],
                }

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("use a tool", agent_id="agent-a")

        assert executed == ["t"]
        assert response.text == "done"
        assert len(transport.requests) == 2
        # The tool result was appended to the conversation, not discarded.
        assert any(m.get("role") == "tool" for m in transport.requests[1]["messages"])

    @pytest.mark.asyncio
    async def test_the_loop_is_bounded(self, vault, transport, monkeypatch):
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        for _ in range(30):
            transport.queue.append(
                (
                    _completion(
                        tool_calls=[
                            {
                                "id": "c",
                                "function": {"name": "t", "arguments": "{}"},
                            }
                        ]
                    ),
                    200,
                )
            )

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, *a, **k):
                return [{"name": "t", "inputSchema": {}}]

            async def call_tool(self, name, arguments):
                return [{"type": "text", "text": "again"}]

            def mcp_to_kimi_tools(self, tools):
                return [{"type": "function", "function": {"name": "t"}}]

            def format_tool_result(self, tool_call_id, content, is_error=False):
                return {"role": "tool", "tool_call_id": tool_call_id, "content": "x"}

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({"max_tool_iterations": 3})
        await backend.initialize()
        response = await backend.run("loop", agent_id="agent-a")
        # Exactly bounded: max_tool_iterations=3 must make exactly three
        # completion calls, not "at most some slack" — an off-by-one
        # running a fourth iteration should fail this.
        assert len(transport.requests) == 3
        assert response.is_error is True

    @pytest.mark.asyncio
    async def test_a_failed_tool_is_surfaced_to_the_model_and_the_loop_continues(
        self, vault, transport, monkeypatch
    ):
        """_run_tool's except branch: a failed call_tool must not crash the
        turn, and the model has to actually see the failure to react to
        it — not just "no exception". The mock's format_tool_result marks
        the error the same way the real ToolAdapter does (an "Error: "
        prefix), so the assertion can tell an error tool-message from an
        ordinary one.
        """
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append(
            (
                _completion(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "t", "arguments": "{}"},
                        }
                    ]
                ),
                200,
            )
        )
        transport.queue.append((_completion(content="done"), 200))

        class _FailingAdapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, *a, **k):
                return [{"name": "t", "description": "", "inputSchema": {}}]

            async def call_tool(self, name, arguments):
                raise RuntimeError("MCP gateway unreachable")

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            def format_tool_result(self, tool_call_id, content, is_error=False):
                text = content[0]["text"]
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": f"Error: {text}" if is_error else text,
                }

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _FailingAdapter)
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("use a tool", agent_id="agent-a")

        # The loop continued past the failure: a second completion was
        # requested, and the turn ended clean because that one succeeded.
        assert len(transport.requests) == 2
        assert response.text == "done"
        assert response.is_error is False

        # The failure reached the model as a tool result, not a runner
        # error — that's the property; "no exception" alone would miss it.
        tool_messages = [
            m for m in transport.requests[1]["messages"] if m.get("role") == "tool"
        ]
        assert tool_messages, "the failed call must still produce a tool message"
        assert tool_messages[0]["content"].startswith("Error:")
        assert "MCP gateway unreachable" in tool_messages[0]["content"]

    @pytest.mark.asyncio
    async def test_cost_accumulates_across_tool_loop_iterations(
        self, vault, transport, monkeypatch
    ):
        """total_usage sums every iteration and feeds the final cost_usd.
        A single-completion cost test (TestPlainCompletion) can't catch a
        regression that drops intermediate usage — this needs at least two
        completions, each carrying real usage, with a hand-checkable rate.
        """
        from plugins.kimi import api_backend, cost_tracker
        from plugins.kimi.api_backend import KimiApiBackend

        monkeypatch.setattr(
            cost_tracker, "KIMI_PRICING", [("kimi-cost-loop", 1.0, 2.0)]
        )

        transport.queue.append(
            (
                _completion(
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "t", "arguments": "{}"},
                        }
                    ],
                    usage={"prompt_tokens": 500_000, "completion_tokens": 0},
                ),
                200,
            )
        )
        transport.queue.append(
            (
                _completion(
                    content="done",
                    usage={
                        "prompt_tokens": 500_000,
                        "completion_tokens": 1_000_000,
                    },
                ),
                200,
            )
        )

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, *a, **k):
                return [{"name": "t", "description": "", "inputSchema": {}}]

            async def call_tool(self, name, arguments):
                return [{"type": "text", "text": "tool output"}]

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            def format_tool_result(self, tool_call_id, content, is_error=False):
                return {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content[0]["text"],
                }

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({"model": "kimi-cost-loop"})
        await backend.initialize()
        response = await backend.run("use a tool", agent_id="agent-a")

        assert response.text == "done"
        assert len(transport.requests) == 2
        # 1M prompt tokens total @ $1/M + 1M completion tokens total @
        # $2/M = $3.00 — only correct if both iterations' usage was
        # summed. Costing only the last completion would give $2.50;
        # only the first, $0.50.
        assert response.cost_usd == pytest.approx(3.0)


class TestErrors:
    @pytest.mark.asyncio
    async def test_a_401_is_reported_as_an_error(self, vault, transport):
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append(({"error": {"message": "invalid api key"}}, 401))
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("hi")
        assert response.is_error is True
        # Not retried: a rejected credential will not heal in 200ms.
        assert len(transport.requests) == 1

    @pytest.mark.asyncio
    async def test_no_api_key_fails_with_the_named_message(self, monkeypatch):
        from plugins.kimi.api_backend import KimiApiBackend

        class _Empty:
            def get_plain(self, key):
                return None

            def set(self, key, value):
                pass

            def delete(self, key):
                pass

        monkeypatch.setattr("ui.secrets_manager.secrets_manager", _Empty())
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("hi")
        assert response.is_error is True
        assert "MOONSHOT_API_KEY" in response.text

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_retried_three_times_in_total(
        self, vault, transport, monkeypatch
    ):
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        # The real `await asyncio.sleep(2**attempt)` between retries costs
        # this one test ~3s (1 + 2 = 3s of real backoff) — 81% of the whole
        # suite's runtime. The retry *count* is what this test pins, not
        # the backoff duration, so the sleep is collapsed to zero.
        #
        # `asyncio` here is the actual stdlib module (api_backend just
        # imports it), and `backend.initialize()` below starts a background
        # cleanup task (core/runners/session_manager.py) whose own loop
        # also calls `asyncio.sleep` to throttle itself every 5 minutes. A
        # no-op replacement with no real suspension point removes that
        # task's only yield point, turning it into a tight busy loop that
        # live-locks the event loop instead of speeding anything up.
        # Delegating to the real `asyncio.sleep(0)` keeps a genuine
        # cooperative yield while still collapsing the delay to ~nothing.
        real_sleep = api_backend.asyncio.sleep

        async def _fast_sleep(seconds):
            await real_sleep(0)

        monkeypatch.setattr(api_backend.asyncio, "sleep", _fast_sleep)

        for _ in range(3):
            transport.queue.append(({"error": {"message": "overloaded"}}, 503))
        backend = KimiApiBackend({"max_retries": 2})
        await backend.initialize()
        response = await backend.run("hi")
        assert len(transport.requests) == 3, "max_retries=2 means three attempts"
        assert response.is_error is True


class TestToolBudgetAndLoading:
    """max_tools and tool_loading, forwarded from run()'s kwargs through to
    ToolAdapter.list_tools — and truncated locally as a safety net when the
    gateway doesn't honour the budget itself.

    Before this fix, `_load_tools` called `list_tools()` with no arguments
    at all: an agent configured tool_loading="search" got the full tool
    block anyway (defeating the whole point of search mode), and an agent
    configured max_tools=30 got everything the gateway had.
    """

    @pytest.mark.asyncio
    async def test_a_budget_is_forwarded_and_honoured_as_a_safety_net(
        self, vault, transport, monkeypatch
    ):
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append((_completion(content="ok"), 200))

        calls = []

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, tool_budget=None, tool_loading="full"):
                calls.append((tool_budget, tool_loading))
                # A gateway that does NOT enforce the budget itself — this
                # is exactly what the local safety-net truncation exists
                # for: 5 tools come back against a budget of 2.
                return [{"name": f"t{i}", "inputSchema": {}} for i in range(5)]

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({})
        await backend.initialize()
        await backend.run("hi", agent_id="agent-a", max_tools=2)

        assert calls == [(2, "full")]
        assert len(transport.requests[0]["tools"]) == 2

    @pytest.mark.asyncio
    async def test_tool_loading_is_forwarded_to_list_tools(
        self, vault, transport, monkeypatch
    ):
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append((_completion(content="ok"), 200))

        calls = []

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, tool_budget=None, tool_loading="full"):
                calls.append((tool_budget, tool_loading))
                return [{"name": "search_tools", "inputSchema": {}}]

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({})
        await backend.initialize()
        await backend.run("hi", agent_id="agent-a", tool_loading="search")

        assert calls == [(None, "search")]

    @pytest.mark.asyncio
    async def test_the_default_with_neither_kwarg_still_loads_tools_as_today(
        self, vault, transport, monkeypatch
    ):
        """No max_tools, no tool_loading passed — the pre-existing default
        behaviour must survive unchanged: tools still load, unbudgeted,
        "full" loading.
        """
        from plugins.kimi import api_backend
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append((_completion(content="ok"), 200))

        calls = []

        class _Adapter:
            async def initialize(self, agent_id, unified_id=None):
                pass

            async def list_tools(self, tool_budget=None, tool_loading="full"):
                calls.append((tool_budget, tool_loading))
                return [{"name": "t", "inputSchema": {}}]

            def mcp_to_kimi_tools(self, tools):
                return [
                    {"type": "function", "function": {"name": t["name"]}} for t in tools
                ]

            async def shutdown(self):
                pass

        monkeypatch.setattr(api_backend, "ToolAdapter", _Adapter)
        backend = KimiApiBackend({})
        await backend.initialize()
        await backend.run("hi", agent_id="agent-a")

        assert calls == [(None, "full")]
        assert len(transport.requests[0]["tools"]) == 1


class TestModelIdResolution:
    """The request's `model` field carries the api_id Moonshot bills under,
    never the UI id the operator picks from the dropdown — even though
    cost_for_turn keeps costing from the UI id (it resolves internally, and
    resolving twice would be redundant, not wrong, but there is no reason
    to). Before this fix, the UI id was posted verbatim: an operator-curated
    alias whose api_id differs (e.g. "kimi-latest" -> "kimi-k2.7-code")
    would 400 on every turn while the cost table priced it correctly.
    """

    @pytest.mark.asyncio
    async def test_the_wire_body_carries_the_api_id_not_the_ui_id(
        self, vault, transport, tmp_path, monkeypatch
    ):
        import core.registry  # noqa: F401 — force import before monkeypatch
        from core.models_registry import ModelsRegistry
        from plugins.kimi.api_backend import KimiApiBackend

        registry = ModelsRegistry(base_dir=tmp_path)
        registry.set_models(
            "kimi",
            [
                {
                    "id": "kimi-latest",
                    "name": "Kimi Latest",
                    "api_id": "kimi-k2.7-code",
                }
            ],
        )
        monkeypatch.setattr("core.registry.get_models_registry", lambda: registry)

        transport.queue.append((_completion(content="ok"), 200))
        backend = KimiApiBackend({"model": "kimi-latest"})
        await backend.initialize()
        await backend.run("hi")

        assert transport.requests[0]["model"] == "kimi-k2.7-code"


class TestFailedTurnNotCommitted:
    """A turn that ends in an error must not leave its user message in
    session history — otherwise the next successful turn re-posts every
    failed prompt as a pile of stale, already-unanswered user messages.
    """

    @pytest.mark.asyncio
    async def test_a_failed_turns_prompt_does_not_leak_into_the_next_request(
        self, vault, transport
    ):
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append(({"error": {"message": "invalid api key"}}, 401))
        transport.queue.append((_completion(content="hi there"), 200))

        backend = KimiApiBackend({})
        await backend.initialize()

        first = await backend.run("this one fails")
        assert first.is_error is True

        second = await backend.run("this one succeeds", session_id=first.session_id)
        assert second.is_error is False

        second_request_messages = transport.requests[1]["messages"]
        contents = [m.get("content") for m in second_request_messages]
        assert "this one fails" not in contents
        assert contents == ["this one succeeds"]
