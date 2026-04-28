"""Tests for Claude API backend (mocked Anthropic SDK)."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.interfaces.runner import RunnerResponse


def _make_text_block(text):
    """Create a mock text content block."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(tool_id, name, tool_input):
    """Create a mock tool_use content block."""
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = tool_input
    return block


def _make_response(content_blocks, input_tokens=100, output_tokens=50):
    """Create a mock Claude API response."""
    response = MagicMock()
    response.content = content_blocks
    response.usage = MagicMock()
    response.usage.input_tokens = input_tokens
    response.usage.output_tokens = output_tokens
    return response


@pytest.fixture
def mock_anthropic():
    """Mock anthropic module before importing ClaudeApiBackend."""
    mock_module = MagicMock()
    with (
        patch.dict("sys.modules", {"anthropic": mock_module}),
        patch("plugins.claude.api_backend.calculate_cost", return_value=0.001),
    ):
        yield mock_module


@pytest.fixture
def backend(mock_anthropic):
    """Create a ClaudeApiBackend with mocked client."""
    from plugins.claude.api_backend import ClaudeApiBackend

    b = ClaudeApiBackend({"model": "sonnet", "timeout": 30})

    mock_client = AsyncMock()
    text_response = _make_response([_make_text_block("Hello from Claude!")])
    mock_client.messages.create = AsyncMock(return_value=text_response)
    b._client = mock_client

    return b, mock_client, text_response


class TestClaudeApiBackendInit:
    """Tests for backend initialization."""

    def test_default_config(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("CLAUDE_MODEL", None)
            b = ClaudeApiBackend({})
        assert b.model == "sonnet"
        assert b.timeout == 900
        assert b.max_output_tokens == 8192
        assert b.max_tool_iterations == 20

    def test_custom_config(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend(
            {
                "model": "opus",
                "timeout": 60,
                "max_output_tokens": 4096,
                "max_tool_iterations": 10,
            }
        )
        assert b.model == "opus"
        assert b.timeout == 60
        assert b.max_output_tokens == 4096
        assert b.max_tool_iterations == 10


class TestResolveModel:
    """Tests for model name resolution."""

    def test_short_names(self, mock_anthropic):
        # Resolve short names using the current default map so the test
        # tracks new model versions instead of hardcoding outdated IDs.
        from plugins.claude.api_backend import _DEFAULT_MODEL_MAP, resolve_model

        for short, full in _DEFAULT_MODEL_MAP.items():
            assert resolve_model(short) == full

    def test_full_id_passthrough(self, mock_anthropic):
        from plugins.claude.api_backend import resolve_model

        # Any string that does not match a short alias is returned as-is.
        full_id = "claude-sonnet-some-future-revision"
        assert resolve_model(full_id) == full_id

    def test_unknown_name_passthrough(self, mock_anthropic):
        from plugins.claude.api_backend import resolve_model

        assert resolve_model("custom-model") == "custom-model"


class TestClaudeApiBackendRun:
    """Tests for run() method."""

    async def test_basic_run(self, backend):
        b, mock_client, _ = backend
        result = await b.run(prompt="Hello")

        assert isinstance(result, RunnerResponse)
        assert result.text == "Hello from Claude!"
        assert result.is_error is False
        assert result.cost_usd == 0.001
        assert result.raw["runner"] == "claude-api"
        mock_client.messages.create.assert_called_once()

    async def test_returns_session_id(self, backend):
        b, _, _ = backend
        result = await b.run(prompt="Hello")
        assert result.session_id is not None
        assert len(result.session_id) > 0

    async def test_session_continuity(self, backend):
        b, _, _ = backend
        r1 = await b.run(prompt="Hello")
        r2 = await b.run(prompt="How are you?", session_id=r1.session_id)
        assert r1.session_id == r2.session_id

    async def test_model_override(self, backend, mock_anthropic):
        from plugins.claude.api_backend import resolve_model

        b, mock_client, _ = backend
        await b.run(prompt="Test", model="opus")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == resolve_model("opus")

    async def test_default_model_used(self, backend, mock_anthropic):
        from plugins.claude.api_backend import resolve_model

        b, mock_client, _ = backend
        await b.run(prompt="Test")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == resolve_model("sonnet")

    async def test_no_tools_skips_tool_setup(self, backend):
        b, _, _ = backend
        with patch.object(b, "_setup_tools") as mock_setup:
            result = await b.run(prompt="Test", no_tools=True, agent_id="myagent")
            mock_setup.assert_not_called()
        assert result.is_error is False

    async def test_system_prompt_passed(self, backend):
        b, mock_client, _ = backend
        await b.run(prompt="Hello", system_prompt="You are a helper")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "You are a helper"

    async def test_no_system_prompt_omits_system(self, backend):
        b, mock_client, _ = backend
        await b.run(prompt="Hello")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "system" not in call_kwargs or call_kwargs.get("system") == ""

    async def test_client_not_initialized(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({})
        b._client = None
        result = await b.run(prompt="Test")

        assert result.is_error is True
        assert "not initialized" in result.text

    async def test_client_not_initialized_calls_error_callback(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({})
        b._client = None
        error_cb = AsyncMock()
        await b.run(prompt="Test", error_callback=error_cb)

        error_cb.assert_called_once()

    async def test_api_exception(self, backend):
        b, mock_client, _ = backend
        mock_client.messages.create = AsyncMock(
            side_effect=Exception("API quota exceeded")
        )
        result = await b.run(prompt="Test")

        assert result.is_error is True
        assert "API quota exceeded" in result.text

    async def test_api_exception_calls_error_callback(self, backend):
        b, mock_client, _ = backend
        mock_client.messages.create = AsyncMock(side_effect=Exception("Network error"))
        error_cb = AsyncMock()
        await b.run(prompt="Test", error_callback=error_cb)

        error_cb.assert_called_once()
        assert "Network error" in error_cb.call_args[0][1]

    async def test_empty_response_text(self, backend):
        b, mock_client, _ = backend
        empty_response = _make_response([_make_text_block("")])
        mock_client.messages.create = AsyncMock(return_value=empty_response)

        result = await b.run(prompt="Test")
        assert result.is_error is False
        assert result.text == ""

    async def test_no_usage_metadata(self, backend):
        b, mock_client, _ = backend
        response = _make_response([_make_text_block("Hello")])
        response.usage = None
        mock_client.messages.create = AsyncMock(return_value=response)

        with patch("plugins.claude.api_backend.calculate_cost") as mock_cost:
            result = await b.run(prompt="Test")
            mock_cost.assert_not_called()

        assert result.cost_usd == 0.0


class TestClaudeApiBackendToolCalls:
    """Tests for the tool call loop."""

    async def test_single_tool_call(self, backend):
        """Tool call -> tool result -> final text response."""
        b, mock_client, _ = backend

        # First response: tool_use
        tc_response = _make_response(
            [
                _make_text_block("Let me search..."),
                _make_tool_use_block("toolu_123", "odoo__search", {"query": "test"}),
            ]
        )

        # Second response: final text
        text_response = _make_response([_make_text_block("Found 3 records")])

        mock_client.messages.create = AsyncMock(
            side_effect=[tc_response, text_response]
        )

        b._tool_adapter.call_tool = AsyncMock(
            return_value=[{"type": "text", "text": "3 records found"}]
        )
        b._tool_adapter.initialize = AsyncMock()
        b._tool_adapter.list_tools = AsyncMock(
            return_value=[
                {
                    "name": "odoo__search",
                    "description": "Search",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )

        result = await b.run(prompt="Search Odoo", agent_id="myagent")

        assert result.text == "Found 3 records"
        assert result.is_error is False
        assert result.raw["tool_iterations"] == 1
        b._tool_adapter.call_tool.assert_called_once_with(
            "odoo__search", {"query": "test"}
        )

    async def test_tool_callback_called(self, backend):
        """tool_callback is invoked for each tool call."""
        b, mock_client, _ = backend

        tc_response = _make_response([_make_tool_use_block("toolu_1", "tool1", {})])
        text_response = _make_response([_make_text_block("Done")])

        mock_client.messages.create = AsyncMock(
            side_effect=[tc_response, text_response]
        )

        b._tool_adapter.call_tool = AsyncMock(
            return_value=[{"type": "text", "text": "ok"}]
        )
        b._tool_adapter.initialize = AsyncMock()
        b._tool_adapter.list_tools = AsyncMock(
            return_value=[
                {
                    "name": "tool1",
                    "description": "T",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )

        tool_cb = AsyncMock()
        await b.run(prompt="Test", agent_id="myagent", tool_callback=tool_cb)

        tool_cb.assert_called_once_with("tool1", {})

    async def test_max_tool_iterations(self, backend):
        """Exceeding max_tool_iterations returns error."""
        b, mock_client, _ = backend
        b.max_tool_iterations = 2

        # Always return tool_use (infinite loop scenario)
        tc_response = _make_response(
            [_make_tool_use_block("toolu_loop", "loop_tool", {})]
        )
        mock_client.messages.create = AsyncMock(return_value=tc_response)

        b._tool_adapter.call_tool = AsyncMock(
            return_value=[{"type": "text", "text": "ok"}]
        )
        b._tool_adapter.initialize = AsyncMock()
        b._tool_adapter.list_tools = AsyncMock(
            return_value=[
                {
                    "name": "loop_tool",
                    "description": "T",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )

        result = await b.run(prompt="Test", agent_id="myagent")

        assert result.is_error is True
        assert "Max tool iterations" in result.text
        assert result.raw["reason"] == "max_tool_iterations"

    async def test_unknown_tool_rejected_client_side(self, backend):
        """Tool calls with names not in definitions are rejected locally."""
        b, mock_client, _ = backend

        # Model hallucinates a tool name not in the definitions
        tc_response = _make_response(
            [_make_tool_use_block("toolu_bad", "fake_tool__search", {"q": "x"})]
        )
        text_response = _make_response(
            [_make_text_block("Sorry, I cannot use that tool")]
        )

        mock_client.messages.create = AsyncMock(
            side_effect=[tc_response, text_response]
        )

        b._tool_adapter.call_tool = AsyncMock()
        b._tool_adapter.initialize = AsyncMock()
        b._tool_adapter.list_tools = AsyncMock(
            return_value=[
                {
                    "name": "real_tool__search",
                    "description": "Real tool",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        )

        result = await b.run(prompt="Search", agent_id="myagent")

        # Gateway should NOT be called for the hallucinated tool
        b._tool_adapter.call_tool.assert_not_called()
        assert result.is_error is False

    async def test_no_agent_id_skips_tools(self, backend):
        """Without agent_id, tools are not loaded."""
        b, _, _ = backend
        with patch.object(b, "_setup_tools") as mock_setup:
            result = await b.run(prompt="Test", agent_id=None)
            mock_setup.assert_not_called()
        assert result.is_error is False


class TestClaudeApiBackendExtractors:
    """Tests for _extract_text and _extract_tool_use."""

    def test_extract_text_single_block(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response([_make_text_block("Hello")])
        assert ClaudeApiBackend._extract_text(response) == "Hello"

    def test_extract_text_multiple_blocks(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response(
            [_make_text_block("Part 1"), _make_text_block("Part 2")]
        )
        text = ClaudeApiBackend._extract_text(response)
        assert "Part 1" in text
        assert "Part 2" in text

    def test_extract_text_skips_tool_use(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response(
            [
                _make_text_block("Hello"),
                _make_tool_use_block("id1", "tool", {}),
            ]
        )
        text = ClaudeApiBackend._extract_text(response)
        assert text == "Hello"

    def test_extract_tool_use(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response(
            [
                _make_text_block("Let me search"),
                _make_tool_use_block("toolu_abc", "search", {"q": "test"}),
            ]
        )
        blocks = ClaudeApiBackend._extract_tool_use(response)
        assert len(blocks) == 1
        assert blocks[0]["id"] == "toolu_abc"
        assert blocks[0]["name"] == "search"
        assert blocks[0]["input"] == {"q": "test"}

    def test_extract_no_tool_use(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response([_make_text_block("Just text")])
        assert ClaudeApiBackend._extract_tool_use(response) == []


class TestClaudeApiBackendStreaming:
    """Tests for streaming support."""

    async def test_stream_callback_receives_accumulated_text(self, backend):
        b, mock_client, _ = backend

        # Mock streaming context manager
        text_chunks = ["Hello ", "world!"]
        final_response = _make_response([_make_text_block("Hello world!")])

        mock_stream = AsyncMock()
        mock_stream.text_stream = _async_iter(text_chunks)
        mock_stream.get_final_message = AsyncMock(return_value=final_response)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.messages.stream = MagicMock(return_value=mock_ctx)

        stream_cb = AsyncMock()
        result = await b.run(prompt="Test", stream_callback=stream_cb)

        assert result.text == "Hello world!"
        assert result.is_error is False
        assert stream_cb.call_count == 2
        assert stream_cb.call_args_list[0][0][0] == "Hello "
        assert stream_cb.call_args_list[1][0][0] == "Hello world!"

    async def test_stream_callback_error_does_not_interrupt(self, backend):
        b, mock_client, _ = backend

        final_response = _make_response([_make_text_block("Hello")])

        mock_stream = AsyncMock()
        mock_stream.text_stream = _async_iter(["Hello"])
        mock_stream.get_final_message = AsyncMock(return_value=final_response)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.messages.stream = MagicMock(return_value=mock_ctx)

        stream_cb = AsyncMock(side_effect=Exception("callback error"))
        result = await b.run(prompt="Test", stream_callback=stream_cb)

        assert result.text == "Hello"
        assert result.is_error is False

    async def test_no_stream_callback_uses_unary(self, backend):
        b, mock_client, _ = backend
        result = await b.run(prompt="Test", stream_callback=None)

        assert result.is_error is False
        mock_client.messages.create.assert_called_once()


class TestClaudeApiBackendRetry:
    """Tests for retry with backoff."""

    async def test_retries_on_transient_error(self, backend):
        b, mock_client, _ = backend
        b.max_retries = 1

        text_response = _make_response([_make_text_block("Hello!")])
        mock_client.messages.create = AsyncMock(
            side_effect=[Exception("429 rate limit exceeded"), text_response]
        )

        with patch("plugins.claude.api_backend.asyncio.sleep", new_callable=AsyncMock):
            result = await b.run(prompt="Test")

        assert result.is_error is False
        assert result.text == "Hello!"
        assert mock_client.messages.create.call_count == 2

    async def test_no_retry_on_non_transient_error(self, backend):
        b, mock_client, _ = backend
        b.max_retries = 2

        mock_client.messages.create = AsyncMock(
            side_effect=Exception("Invalid argument: bad model name")
        )

        result = await b.run(prompt="Test")

        assert result.is_error is True
        assert mock_client.messages.create.call_count == 1

    async def test_retry_exhausted(self, backend):
        b, mock_client, _ = backend
        b.max_retries = 2

        mock_client.messages.create = AsyncMock(side_effect=Exception("503 overloaded"))

        with patch("plugins.claude.api_backend.asyncio.sleep", new_callable=AsyncMock):
            result = await b.run(prompt="Test")

        assert result.is_error is True
        assert "503 overloaded" in result.text
        assert mock_client.messages.create.call_count == 3


class TestClaudeApiBackendIsTransientError:
    """Tests for _is_transient_error()."""

    def test_rate_limit(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        assert ClaudeApiBackend._is_transient_error(Exception("429 rate limit"))

    def test_overloaded(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        assert ClaudeApiBackend._is_transient_error(Exception("overloaded"))

    def test_529(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        assert ClaudeApiBackend._is_transient_error(Exception("529 API overloaded"))

    def test_timeout(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        assert ClaudeApiBackend._is_transient_error(Exception("Connection timeout"))

    def test_non_transient(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        assert not ClaudeApiBackend._is_transient_error(Exception("Invalid argument"))


class TestClaudeApiBackendShutdown:
    """Tests for shutdown."""

    async def test_shutdown_cleans_up(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({})
        b._client = AsyncMock()
        b._tool_adapter.shutdown = AsyncMock()
        b._sessions.stop_cleanup_loop = AsyncMock()

        await b.shutdown()

        assert b._client is None
        b._tool_adapter.shutdown.assert_called_once()
        b._sessions.stop_cleanup_loop.assert_called_once()


class TestClaudeApiBackendFormatAssistantMessage:
    """Tests for _format_assistant_message()."""

    def test_text_only(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response([_make_text_block("Hello")])
        msg = ClaudeApiBackend._format_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][0]["text"] == "Hello"

    def test_text_and_tool_use(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        response = _make_response(
            [
                _make_text_block("I'll search"),
                _make_tool_use_block("toolu_1", "search", {"q": "x"}),
            ]
        )
        msg = ClaudeApiBackend._format_assistant_message(response)
        assert msg["role"] == "assistant"
        assert len(msg["content"]) == 2
        assert msg["content"][0]["type"] == "text"
        assert msg["content"][1]["type"] == "tool_use"
        assert msg["content"][1]["id"] == "toolu_1"
        assert msg["content"][1]["name"] == "search"
        assert msg["content"][1]["input"] == {"q": "x"}


# --- Helpers ---


async def _async_iter(items):
    """Helper to create an async iterable from a list."""
    for item in items:
        yield item


class TestClaudeApiBackendMaxTools:
    """Tests for max_tools config."""

    def test_max_tools_default_unlimited(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({})
        assert b.max_tools == 0

    def test_max_tools_from_config(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({"max_tools": 50})
        assert b.max_tools == 50

    @pytest.mark.asyncio
    async def test_setup_tools_truncates_when_over_limit(self, mock_anthropic):
        from plugins.claude.api_backend import ClaudeApiBackend

        b = ClaudeApiBackend({"max_tools": 2})
        b._tool_adapter.initialize = AsyncMock()
        b._tool_adapter.list_tools = AsyncMock(
            return_value=[
                {"name": "t1", "description": "d1", "inputSchema": {}},
                {"name": "t2", "description": "d2", "inputSchema": {}},
                {"name": "t3", "description": "d3", "inputSchema": {}},
            ]
        )
        b._tool_adapter.mcp_to_claude_tools = MagicMock(
            return_value=[
                {"name": "t1", "description": "d1", "input_schema": {}},
                {"name": "t2", "description": "d2", "input_schema": {}},
            ]
        )

        await b._setup_tools("test-agent")

        call_args = b._tool_adapter.mcp_to_claude_tools.call_args[0][0]
        assert len(call_args) == 2
