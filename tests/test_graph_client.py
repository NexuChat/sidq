from mcp.types import CallToolResult, TextContent

from sidq.graph.client import _tool_response_payload


def test_mcp_2_tool_result_uses_snake_case_response_fields() -> None:
    response = CallToolResult(
        content=[TextContent(type="text", text='{"fallback": false}')],
        structuredContent={"live": True},
        isError=False,
    )

    assert _tool_response_payload(response) == {"live": True}
