from __future__ import annotations

import json

import pytest
from mcp.types import CallToolResult, TextContent

from sidq.graph.client import (
    DatasetInfo,
    GraphResponseError,
    LineagePath,
    LineageResult,
    MCPGraphClient,
    _mcp_subprocess_environment,
    _tool_response_payload,
)
from sidq.graph.fixtures import RecordingGraphClient, ReplayGraphClient, _lineage

URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.orders,PROD)"
DOWNSTREAM = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.dashboard,PROD)"


def test_read_only_mcp_subprocess_environment_is_closed(monkeypatch) -> None:
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "reader-token")
    monkeypatch.setenv("CLAIMS_SOURCE", "postgresql://reader:secret@warehouse/db")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "ambient-secret")

    environment = _mcp_subprocess_environment("https://catalog.example.test")

    assert environment["DATAHUB_GMS_URL"] == "https://catalog.example.test"
    assert environment["DATAHUB_GMS_TOKEN"] == "reader-token"
    assert environment["DATAHUB_TELEMETRY_ENABLED"] == "false"
    assert environment["LOGURU_LEVEL"] == "WARNING"
    assert "PATH" in environment
    assert "CLAIMS_SOURCE" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "UNRELATED_SECRET" not in environment


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


def test_mcp_tool_response_falls_back_to_the_text_content_as_json() -> None:
    response = _ToolResponse(text=json.dumps({"ok": True}))

    assert _tool_response_payload(response) == {"ok": True}


def test_find_dataset_by_urn_uses_get_entities_not_search() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "list_schema_fields":
            return {"schema_fields": []}
        return {"entities": [{"urn": URN}]}

    assert MCPGraphClient(caller).find_dataset(URN) == URN

    assert [name for name, _ in calls] == ["get_entities", "list_schema_fields"]
    assert calls[0] == ("get_entities", {"urns": [URN]})


def test_find_dataset_by_name_builds_a_scoped_dataset_search() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        return {"results": [{"entity": {"urn": URN}}]}

    assert MCPGraphClient(caller).find_dataset("orders") == URN

    assert calls == [
        (
            "search",
            {"query": "orders", "filter": "entity_type = dataset", "num_results": 50},
        )
    ]


def test_find_dataset_returns_none_when_no_search_result_is_a_dataset() -> None:
    client = MCPGraphClient(
        lambda name, arguments: {"results": [{"entity": {"urn": "urn:li:chart:x"}}]}
    )

    assert client.find_dataset("weekly revenue chart") is None


def test_get_dataset_requests_entities_and_schema_fields_with_fixed_limits() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {"entities": [{"urn": URN}]}
        if name == "list_schema_fields":
            return {"schema_fields": []}
        raise AssertionError(name)

    dataset = MCPGraphClient(caller).get_dataset(URN)

    assert dataset == DatasetInfo(urn=URN)
    assert ("get_entities", {"urns": [URN]}) in calls
    assert ("list_schema_fields", {"urn": URN, "limit": 100}) in calls


def test_get_dataset_treats_a_non_mapping_transport_reply_as_not_found() -> None:
    client = MCPGraphClient(lambda name, arguments: "unexpected-string-reply")

    assert client.get_dataset(URN) is None


def test_get_dataset_rejects_an_entity_that_does_not_identify_its_urn() -> None:
    client = MCPGraphClient(
        lambda name, arguments: {"entities": [{"name": "orders", "tags": []}]}
    )

    assert client.get_dataset(URN) is None


def test_get_dataset_skips_schema_fields_that_have_no_usable_path() -> None:
    def caller(name: str, arguments: dict[str, object]) -> object:
        if name == "get_entities":
            return {"entities": [{"urn": URN}]}
        if name == "list_schema_fields":
            return {"schema_fields": [{"nativeType": "TEXT"}, {"path": None}]}
        raise AssertionError(name)

    dataset = MCPGraphClient(caller).get_dataset(URN)

    assert dataset is not None
    assert dataset.fields == ()


def test_get_downstream_only_sends_a_column_argument_when_one_is_requested() -> None:
    calls: list[dict[str, object]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append(dict(arguments))
        return {"downstreams": {"searchResults": []}, "metadata": {}}

    client = MCPGraphClient(caller)
    client.get_downstream(URN, 2)
    client.get_downstream(URN, 2, column="email")

    assert calls[0] == {
        "urn": URN,
        "upstream": False,
        "max_hops": 2,
        "max_results": 100,
    }
    assert calls[1] == {
        "urn": URN,
        "upstream": False,
        "max_hops": 2,
        "max_results": 100,
        "column": "email",
    }


def test_get_downstream_requests_a_single_fixed_size_page_not_a_pagination_loop() -> (
    None
):
    calls: list[dict[str, object]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append(dict(arguments))
        # A transport that always looks like there is more data than one page
        # holds; if the client paginated, it would call again for the rest.
        return {
            "downstreams": {"searchResults": [{"entity": {"urn": DOWNSTREAM}}]},
            "metadata": {},
        }

    MCPGraphClient(caller).get_downstream(URN, 3)

    assert len(calls) == 1
    assert calls[0]["max_results"] == 100


def test_get_downstream_preserves_bounded_response_completeness() -> None:
    client = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {
                "total": 143,
                "returned": 100,
                "searchResults": [{"entity": {"urn": DOWNSTREAM}}],
            },
            "metadata": {"queryType": "table-lineage"},
        }
    )

    result = client.get_downstream(URN, 3)

    assert result.total == 143
    assert result.returned == 100
    assert result.complete is False


def test_get_downstream_marks_a_fully_returned_response_complete() -> None:
    client = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {
                "total": 1,
                "returned": 1,
                "hasMore": False,
                "searchResults": [{"entity": {"urn": DOWNSTREAM}}],
            },
            "metadata": {"queryType": "table-lineage"},
        }
    )

    result = client.get_downstream(URN, 3)

    assert result.total == result.returned == 1
    assert result.complete is True


def test_get_downstream_accepts_the_official_zero_lineage_shape() -> None:
    result = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {
                "total": 0,
                "facets": [{"field": "degree", "aggregations": []}],
            }
        }
    ).get_downstream(URN, 3)

    assert result.urns == ()
    assert result.total == result.returned == 0
    assert result.complete is True


@pytest.mark.parametrize(
    "downstreams",
    [
        {"total": 0, "returned": 0},
        {"total": 0, "searchResults": []},
        {"total": 0, "hasMore": False},
        {"total": 0, "has_more": False},
        {"total": False},
        {"total": 1, "facets": []},
    ],
)
def test_get_downstream_rejects_near_misses_of_the_official_empty_shape(
    downstreams: dict[str, object],
) -> None:
    client = MCPGraphClient(lambda name, arguments: {"downstreams": downstreams})

    try:
        result = client.get_downstream(URN, 3)
    except GraphResponseError:
        return
    assert result.complete is False


@pytest.mark.parametrize(
    "continuation",
    [
        {"hasMore": True},
        {"has_more": True},
        {"hasMore": "false"},
        {},
    ],
)
def test_get_downstream_requires_an_explicit_false_continuation_marker(
    continuation: dict[str, object],
) -> None:
    result = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {
                "total": 1,
                "returned": 1,
                "searchResults": [{"entity": {"urn": DOWNSTREAM}}],
                **continuation,
            }
        }
    ).get_downstream(URN, 3)

    assert result.complete is False


@pytest.mark.parametrize(
    "search_results",
    [
        [{"entity": {}}],
        [
            {"entity": {"urn": DOWNSTREAM}},
            {"entity": {"urn": DOWNSTREAM}},
        ],
    ],
)
def test_get_downstream_requires_a_unique_usable_urn_for_every_returned_result(
    search_results: list[dict[str, object]],
) -> None:
    count = len(search_results)
    client = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {
                "total": count,
                "returned": count,
                "searchResults": search_results,
            },
            "metadata": {"queryType": "table-lineage"},
        }
    )

    assert client.get_downstream(URN, 3).complete is False


def test_recorded_lineage_replays_completeness_without_laundering_truncation(
    tmp_path,
) -> None:
    truncated = LineageResult(
        urns=(DOWNSTREAM,), total=143, returned=100, complete=False
    )

    class Graph:
        def get_downstream(
            self, urn: str, depth: int, column: str | None = None
        ) -> LineageResult:
            return truncated

    RecordingGraphClient(Graph(), tmp_path).get_downstream(URN, 3)

    assert ReplayGraphClient(tmp_path).get_downstream(URN, 3) == truncated


def test_legacy_bounded_fixture_without_completeness_fails_closed() -> None:
    result = _lineage({"urns": [DOWNSTREAM], "total": 143, "returned": 100})

    assert result.total == 143
    assert result.returned == 100
    assert result.complete is False


@pytest.mark.parametrize("explicit_complete", [None, True])
def test_replayed_lineage_counts_must_match_the_recorded_urns(
    explicit_complete: bool | None,
) -> None:
    raw: dict[str, object] = {
        "urns": [DOWNSTREAM],
        "total": 100,
        "returned": 100,
    }
    if explicit_complete is not None:
        raw["complete"] = explicit_complete

    assert _lineage(raw).complete is False


@pytest.mark.parametrize(
    "metadata", [{}, {"queryType": "table-lineage"}, {"queryType": None}]
)
def test_granularity_defaults_to_table_without_the_column_level_marker(
    metadata: dict[str, object],
) -> None:
    def caller(name: str, arguments: dict[str, object]) -> object:
        return {
            "downstreams": {
                "searchResults": [
                    {"entity": {"urn": DOWNSTREAM}, "lineageColumns": ["email"]}
                ]
            },
            "metadata": metadata,
        }

    result = MCPGraphClient(caller).get_downstream(URN, 3, column="email")

    # Column-level data can be *present* in the payload, but queryType is the
    # only marker the client is allowed to trust for the granularity claim.
    assert result.columns == {DOWNSTREAM: ("email",)}
    assert result.granularity == "table"


def test_paths_between_only_sends_column_arguments_when_both_are_given() -> None:
    calls: list[dict[str, object]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append(dict(arguments))
        return {"paths": []}

    client = MCPGraphClient(caller)
    client.paths_between(URN, DOWNSTREAM)
    client.paths_between(URN, DOWNSTREAM, "email", "email")

    assert "source_column" not in calls[0] and "target_column" not in calls[0]
    assert calls[1]["source_column"] == "email"
    assert calls[1]["target_column"] == "email"


def test_paths_between_discards_paths_whose_endpoints_do_not_match() -> None:
    other = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.other,PROD)"

    def caller(name: str, arguments: dict[str, object]) -> object:
        return {"paths": [{"path": [URN, other]}]}

    assert MCPGraphClient(caller).paths_between(URN, DOWNSTREAM) == []


def test_paths_between_granularity_defaults_to_table_without_the_path_type_marker() -> (
    None
):
    def caller(name: str, arguments: dict[str, object]) -> object:
        return {"metadata": {}, "paths": [{"path": [URN, DOWNSTREAM]}]}

    paths = MCPGraphClient(caller).paths_between(URN, DOWNSTREAM)

    assert paths == [LineagePath((URN, DOWNSTREAM), "table")]


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.get_dataset(URN),
        lambda client: client.find_dataset("orders"),
        lambda client: client.paths_between(URN, DOWNSTREAM),
    ],
)
def test_transport_errors_propagate_out_of_every_query_method(call) -> None:
    def unreachable(name: str, arguments: object) -> object:
        raise ConnectionError("datahub gms unreachable")

    with pytest.raises(ConnectionError, match="unreachable"):
        call(MCPGraphClient(unreachable))


def test_get_downstream_signals_failure_via_exception_not_a_quiet_empty_result() -> (
    None
):
    """The only way today to distinguish "no downstream consumers" from "we could
    not ask" is a raised exception -- there is no typed failure value. Every gate
    relies on exactly this: they wrap graph calls in try/except and only emit
    graph_unavailable evidence when an exception actually crosses this boundary."""

    def unreachable(name: str, arguments: object) -> object:
        raise ConnectionError("datahub gms unreachable")

    with pytest.raises(ConnectionError):
        MCPGraphClient(unreachable).get_downstream(URN, 3)


def test_a_malformed_lineage_payload_is_distinguishable_from_a_genuinely_empty_one() -> (
    None
):
    malformed = MCPGraphClient(lambda name, arguments: {"unexpected": "shape"})
    well_formed_empty = MCPGraphClient(
        lambda name, arguments: {
            "downstreams": {"searchResults": []},
            "metadata": {"queryType": "table-lineage"},
        }
    )

    with pytest.raises(GraphResponseError, match="downstreams object"):
        malformed.get_downstream(URN, 3)
    assert well_formed_empty.get_downstream(URN, 3).urns == ()


def test_close_delegates_to_a_tool_caller_that_defines_it() -> None:
    class Caller:
        def __init__(self) -> None:
            self.closed = False

        def __call__(self, name: str, arguments: object) -> object:
            raise AssertionError("close() must not invoke the tool caller")

        def close(self) -> None:
            self.closed = True

    caller = Caller()
    client = MCPGraphClient(caller)

    client.close()

    assert caller.closed is True


def test_close_is_a_no_op_for_a_plain_callable_transport() -> None:
    MCPGraphClient(lambda name, arguments: {}).close()  # must not raise
