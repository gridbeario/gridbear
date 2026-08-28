"""A minimal MCP server, spawned over stdio by the round-trip test.

Deliberately built the way the plugin servers are — `Tool(inputSchema=...)`
declared through `mcp.server.Server`, served by `stdio_server` — so a breaking
change in the SDK surfaces here rather than in production.
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

ECHO_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "text to send back"},
    },
    "required": ["message"],
}

server = Server("echo-server")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Return the message that was passed in.",
            inputSchema=ECHO_SCHEMA,
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "echo":
        raise ValueError(f"unknown tool: {name}")
    return [TextContent(type="text", text=arguments["message"])]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
