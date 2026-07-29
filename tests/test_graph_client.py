from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, TextContent

from sidq.graph.client import LineagePath, MCPGraphClient, _tool_response_payload

URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.orders,PROD)"
DOWNSTREAM = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.dashboard,PROD)"


def test_mcp_2_tool_result_uses_snake_case_response_fields() -> None:
    response = CallToolResult(
        content=[TextContent(type="text", text='{"fallback": false}')],
        structuredContent={"live": True},
        isError=False,
    )

    assert _tool_response_payload(response) == {"live": True}


def test_mcp_graph_client_parses_live_tool_shapes_and_caches_results() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "search":
            return {"results": [{"entity": {"urn": URN}}]}
        if name == "get_entities":
            return {
                "entities": [
                    {
                        "urn": URN,
                        "tags": [{"urn": "urn:li:tag:PII"}],
                        "ownership": [{"owner": {"urn": "urn:li:corpuser:alice"}}],
                        "deprecated": "true",
                    }
                ]
            }
        if name == "list_schema_fields":
            return {
                "schema_fields": [
                    {"path": "email", "nativeType": "TEXT", "isNullable": "false"},
                    {"path": 42},
                ]
            }
        if name == "get_lineage":
            return {
                "downstreams": {
                    "searchResults": [
                        {
                            "entity": {
                                "urn": DOWNSTREAM,
                                "type": "dataset",
                                "tags": ["urn:li:tag:restricted"],
                            },
                            "lineageColumns": ["email", 7],
                        }
                    ]
                },
                "paths": [{"path": [URN, DOWNSTREAM]}],
                "metadata": {"queryType": "column-level-lineage"},
            }
        if name == "get_lineage_paths_between":
            return {
                "metadata": {"pathType": "column-level"},
                "paths": [
                    {
                        "path": [
                            {
                                "type": "SCHEMA_FIELD",
                                "fieldPath": "email",
                                "parent": {"urn": URN},
                            },
                            {
                                "type": "SCHEMA_FIELD",
                                "fieldPath": "email",
                                "parent": {"urn": DOWNSTREAM},
                            },
                        ]
                    }
                ],
            }
        raise AssertionError(name)

    client = MCPGraphClient(caller)

    assert client.find_dataset("orders") == URN
    dataset = client.get_dataset(URN)
    assert dataset is not None
    assert dataset.fields[0].path == "email"
    assert dataset.fields[0].nullable is False
    assert dataset.owners == ("urn:li:corpuser:alice",)
    assert dataset.deprecated is True
    assert client.find_dataset(URN) == URN
    lineage = client.get_downstream(URN, 3, "email")
    assert lineage.urns == (DOWNSTREAM,)
    assert lineage.columns == {DOWNSTREAM: ("email",)}
    assert lineage.granularity == "column"
    assert client.get_downstream(URN, 3, "email") == lineage
    assert client.paths_between(URN, DOWNSTREAM, "email", "email") == [
        LineagePath(
            (
                # The typed MCP path response preserves the oriented field endpoints.
                "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.orders,PROD),email)",
                "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.dashboard,PROD),email)",
            ),
            "column",
        )
    ]

    assert [name for name, _ in calls].count("get_entities") == 1
    assert [name for name, _ in calls].count("get_lineage") == 1


def test_mcp_graph_client_treats_missing_entity_as_missing_aspect() -> None:
    client = MCPGraphClient(
        lambda name, arguments: {"entities": [{"urn": URN, "error": "not found"}]}
    )

    assert client.get_dataset(URN) is None
    assert client.find_dataset(URN) is None


def test_mcp_graph_client_preserves_transport_timeouts() -> None:
    def timeout(name: str, arguments: object) -> object:
        raise TimeoutError("MCP request timed out")

    with pytest.raises(TimeoutError, match="timed out"):
        MCPGraphClient(timeout).get_downstream(URN, 3)


class _ToolResponse:
    def __init__(self, *, text: str, is_error: bool = False) -> None:
        self.content = [TextContent(type="text", text=text)]
        self.is_error = is_error


def test_mcp_tool_response_rejects_error_and_malformed_payloads() -> None:
    with pytest.raises(RuntimeError, match="MCP get_lineage failed: unavailable"):
        _tool_response_payload(
            _ToolResponse(text="unavailable", is_error=True), name="get_lineage"
        )
    with pytest.raises(json.JSONDecodeError):
        _tool_response_payload(_ToolResponse(text="not-json"))
