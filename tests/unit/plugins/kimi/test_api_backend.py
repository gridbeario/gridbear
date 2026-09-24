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
        assert len(transport.requests) <= 4
        assert response.is_error is True


class TestErrors:
    @pytest.mark.asyncio
    async def test_a_401_is_reported_as_an_error(self, vault, transport):
        from plugins.kimi.api_backend import KimiApiBackend

        transport.queue.append(({"error": {"message": "invalid api key"}}, 401))
        backend = KimiApiBackend({})
        await backend.initialize()
        response = await backend.run("hi")
        assert response.is_error is True

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
        self, vault, transport
    ):
        from plugins.kimi.api_backend import KimiApiBackend

        for _ in range(3):
            transport.queue.append(({"error": {"message": "overloaded"}}, 503))
        backend = KimiApiBackend({"max_retries": 2})
        await backend.initialize()
        response = await backend.run("hi")
        assert len(transport.requests) == 3, "max_retries=2 means three attempts"
        assert response.is_error is True
