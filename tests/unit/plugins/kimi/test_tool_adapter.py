"""MCP tool definitions to OpenAI function-calling shape.

The field the MCP SDK puts the schema in is read by its **wire alias**,
`inputSchema`. mcp 2.0.0 renamed the Python attribute to `input_schema` and
every server returned an empty tool list for hours while the suite stayed
green; the wire name is the stable one.
"""


class TestToolConversion:
    def test_an_mcp_tool_becomes_an_openai_function(self):
        from plugins.kimi.tool_adapter import ToolAdapter

        converted = ToolAdapter().mcp_to_kimi_tools(
            [
                {
                    "name": "gridbear__search",
                    "description": "Search things",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                }
            ]
        )
        assert converted == [
            {
                "type": "function",
                "function": {
                    "name": "gridbear__search",
                    "description": "Search things",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    },
                },
            }
        ]

    def test_a_tool_without_a_schema_gets_an_empty_object_schema(self):
        from plugins.kimi.tool_adapter import ToolAdapter

        converted = ToolAdapter().mcp_to_kimi_tools([{"name": "ping"}])
        assert converted[0]["function"]["parameters"] == {
            "type": "object",
            "properties": {},
        }
        assert converted[0]["function"]["description"] == ""


class TestResultFormatting:
    def test_a_result_becomes_a_tool_role_message(self):
        from plugins.kimi.tool_adapter import ToolAdapter

        message = ToolAdapter().format_tool_result(
            "call_1", [{"type": "text", "text": "done"}]
        )
        assert message == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "done",
        }

    def test_an_error_result_is_prefixed_so_the_model_can_react(self):
        from plugins.kimi.tool_adapter import ToolAdapter

        message = ToolAdapter().format_tool_result(
            "call_1", [{"type": "text", "text": "no such file"}], is_error=True
        )
        assert message["content"] == "Error: no such file"
        # A failed tool is a tool result, not a runner error: the model has to
        # see it to react to it.
        assert message["role"] == "tool"

    def test_a_very_large_result_is_truncated_with_the_total_named(self):
        from plugins.kimi.tool_adapter import ToolAdapter

        huge = "x" * 150_000
        message = ToolAdapter().format_tool_result(
            "call_1", [{"type": "text", "text": huge}]
        )
        assert len(message["content"]) < len(huge)
        assert "150000 chars total" in message["content"]
