"""Tool Adapter: MCP Gateway -> Kimi function calling bridge.

Thin wrapper around BaseToolAdapter. Moonshot's API is OpenAI-compatible, so
the shape is OpenAI function calling. Written here rather than imported from
plugins/openai: a cross-plugin import is against convention, and avoiding it
is the reason this runner is its own plugin.
"""

from config.logging_config import logger
from core.runners.tool_adapter import BaseToolAdapter


class ToolAdapter(BaseToolAdapter):
    """Bridge between the MCP Gateway and Kimi function calling."""

    def mcp_to_kimi_tools(self, mcp_tools: list[dict]) -> list[dict]:
        """Convert MCP tool definitions to function-calling format.

        The schema is read as `inputSchema` — the **wire** name, which is
        stable across MCP SDK versions. mcp 2.0.0 renamed the Python
        attribute and served empty tool lists for hours on the old name.
        """
        tools = []
        for tool in mcp_tools:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get(
                            "inputSchema", {"type": "object", "properties": {}}
                        ),
                    },
                }
            )
        return tools

    def format_tool_result(
        self,
        tool_call_id: str,
        content: list[dict],
        is_error: bool = False,
    ) -> dict:
        """Format an MCP tool response as a Kimi tool message.

        A failed tool is surfaced as a tool *result*, not a runner error, so
        the model can react to it.
        """
        text = "\n".join(self._extract_text_parts(content))

        if is_error and text:
            text = f"Error: {text}"

        max_len = 100_000
        if len(text) > max_len:
            original = len(text)
            text = text[:max_len] + f"\n... (truncated, {original} chars total)"
            logger.warning(
                "Tool result for %s truncated from %d to %d chars",
                tool_call_id,
                original,
                max_len,
            )

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": text,
        }
