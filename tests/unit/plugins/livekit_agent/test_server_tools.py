"""The livekit-agent MCP server, spoken to over stdio as the gateway does.

Its tool schemas used to be hand-written JSON next to a name-dispatch table,
which let the declared contract and the code that read the arguments drift
apart unnoticed. They are now derived from the function signatures, so this
asserts the surface an agent actually receives — names, required arguments and
the per-argument descriptions that guide the model.
"""

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("livekit", reason="the livekit extra is not installed")

from core.mcp_gateway.client_manager import (  # noqa: E402
    MCPClientManager,
    MCPServerConnection,
)
from core.mcp_gateway.provider_loader import ServerInfo  # noqa: E402

SERVER = Path(__file__).resolve().parents[4] / "plugins" / "livekit-agent" / "server.py"
HANDSHAKE_TIMEOUT = 60


async def _tools_by_name() -> dict:
    manager = MCPClientManager()
    conn = MCPServerConnection(
        server_info=ServerInfo(
            server_name="livekit-agent",
            config={"command": sys.executable, "args": [str(SERVER)]},
            transport="stdio",
            provider_name="livekit-agent",
        )
    )
    try:
        await asyncio.wait_for(manager._connect(conn), timeout=HANDSHAKE_TIMEOUT)
        return {t["name"]: t for t in conn.tools}
    finally:
        await manager._cleanup_connection(conn)


@pytest.mark.asyncio
async def test_the_server_exposes_its_four_tools():
    tools = await _tools_by_name()
    assert sorted(tools) == [
        "end_voice_call",
        "get_call_link",
        "list_active_calls",
        "start_voice_call",
    ]


@pytest.mark.asyncio
async def test_required_arguments_match_what_each_tool_needs():
    tools = await _tools_by_name()
    required = {n: t["inputSchema"].get("required", []) for n, t in tools.items()}
    assert required["start_voice_call"] == ["user_id"]
    assert required["end_voice_call"] == ["room_name"]
    assert required["get_call_link"] == ["user_id"]
    assert required["list_active_calls"] == []


@pytest.mark.asyncio
async def test_optional_arguments_stay_optional():
    """user_name and caller_identity are filled in by the caller when known."""
    tools = await _tools_by_name()
    props = tools["start_voice_call"]["inputSchema"]["properties"]
    assert set(props) == {"user_id", "user_name", "caller_identity"}


@pytest.mark.asyncio
async def test_every_argument_carries_a_description():
    """The descriptions are what tell the model how to fill each argument."""
    tools = await _tools_by_name()
    for name, tool in tools.items():
        for arg, spec in tool["inputSchema"].get("properties", {}).items():
            assert spec.get("description"), f"{name}.{arg} has no description"


@pytest.mark.asyncio
async def test_every_tool_is_named_in_the_provider_allowlist():
    """A tool missing from the allowlist never reaches an agent.

    The allowlist is hand-written in the provider, so a tool renamed on the
    server side drops out silently instead of failing.
    """
    import importlib.util

    provider_path = SERVER.with_name("provider.py")
    spec = importlib.util.spec_from_file_location("lk_provider", provider_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    allowed = {
        name.rsplit("__", 1)[-1]
        for name in module.LiveKitProvider({}).get_allowed_tools()
    }

    tools = await _tools_by_name()
    assert set(tools) <= allowed, f"not allowlisted: {set(tools) - allowed}"
