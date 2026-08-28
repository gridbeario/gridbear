"""A minimal MCP server, spawned over stdio by the round-trip test.

Built the way the plugin servers are — the ergonomic API, accepting whichever
major is installed — so a breaking change in the SDK surfaces here rather than
in production.
"""

import asyncio
from typing import Annotated

from pydantic import Field

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _McpServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _McpServer

mcp = _McpServer("echo-server")


@mcp.tool(description="Return the message that was passed in.", structured_output=False)
async def echo(
    message: Annotated[str, Field(description="text to send back")],
) -> str:
    return message


if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())
