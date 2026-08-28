"""Connect the gateway to a real MCP server over stdio.

Every other test in this area substitutes the session with a mock, which means
none of them exercise the SDK. That gap is not hypothetical: mcp 2.0.0 renamed
`Tool.inputSchema` to `input_schema`, `_connect` raised AttributeError on every
server, and agents were served an empty tool list for hours while the suite
stayed green. These tests spawn an actual subprocess and speak the protocol, so
a change of that shape fails here instead.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from core.mcp_gateway.client_manager import MCPClientManager, MCPServerConnection
from core.mcp_gateway.provider_loader import ServerInfo

ECHO_SERVER = Path(__file__).with_name("echo_server.py")

# Generous: this guards against a hung handshake stalling CI, it is not a
# performance assertion.
HANDSHAKE_TIMEOUT = 60


def _echo_server_info() -> ServerInfo:
    return ServerInfo(
        server_name="echo",
        config={"command": sys.executable, "args": [str(ECHO_SERVER)]},
        transport="stdio",
        provider_name="echo",
    )


async def _connected():
    """Connect to the echo server; caller must clean up."""
    manager = MCPClientManager()
    conn = MCPServerConnection(server_info=_echo_server_info())
    await asyncio.wait_for(manager._connect(conn), timeout=HANDSHAKE_TIMEOUT)
    return manager, conn


@pytest.mark.asyncio
async def test_stdio_handshake_reports_the_server_tools():
    manager, conn = await _connected()
    try:
        assert conn.connected is True
        assert [t["name"] for t in conn.tools] == ["echo"]
    finally:
        await manager._cleanup_connection(conn)


@pytest.mark.asyncio
async def test_tool_schema_survives_the_handshake():
    """The gateway reads the schema off the SDK's Tool object by attribute.

    An empty or missing schema is what agents see as an unusable tool, so assert
    the contents rather than the key's presence.
    """
    manager, conn = await _connected()
    try:
        schema = conn.tools[0]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"]["message"]["type"] == "string"
        assert schema["required"] == ["message"]
    finally:
        await manager._cleanup_connection(conn)


@pytest.mark.asyncio
async def test_tool_description_survives_the_handshake():
    manager, conn = await _connected()
    try:
        assert conn.tools[0]["description"] == "Return the message that was passed in."
    finally:
        await manager._cleanup_connection(conn)


@pytest.mark.asyncio
async def test_a_tool_call_returns_the_servers_result():
    """Listing tools is only half the protocol; calling one is the other half."""
    manager, conn = await _connected()
    try:
        result = await asyncio.wait_for(
            conn.session.call_tool("echo", {"message": "round trip"}),
            timeout=HANDSHAKE_TIMEOUT,
        )
        assert [c.text for c in result.content] == ["round trip"]
    finally:
        await manager._cleanup_connection(conn)


@pytest.mark.asyncio
async def test_cleanup_marks_the_connection_closed():
    manager, conn = await _connected()
    await manager._cleanup_connection(conn)
    assert conn.connected is False
    assert conn.tools == []
