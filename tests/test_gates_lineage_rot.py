from __future__ import annotations

import pytest

from sidq.gates.lineage_rot import LineageRotGate
from sidq.graph.client import DatasetInfo, LineageResult, MCPGraphClient, SchemaField
from sidq.models import FieldRef, TouchedAsset

SOURCE = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.raw_customers,PROD)"
TARGET = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.customers,PROD)"


class Graph:
    def __init__(
        self, claims: dict[str, tuple[str, ...]], *, granularity: str = "column"
    ) -> None:
        self.claims = claims
        self.granularity = granularity

    def find_dataset(self, name: str) -> str | None:
        return SOURCE if name == "analytics.raw_customers" else None

    def get_dataset(self, urn: str) -> DatasetInfo:
        assert urn == TARGET
        return DatasetInfo(
            urn, tuple(SchemaField(column, "TEXT", True) for column in self.claims)
        )

    def get_upstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        assert urn == TARGET
        assert depth == 1
        assert column is not None
        return LineageResult(
            urns=(SOURCE,),
            columns={SOURCE: self.claims.get(column, ())},
            granularity=self.granularity,
        )


def _asset() -> TouchedAsset:
    return TouchedAsset(
        TARGET,
        "unused.sql",
        (),
        (),
        (
            FieldRef(SOURCE, "id"),
            FieldRef(SOURCE, "email"),
            FieldRef(SOURCE, "legacy_email"),
        ),
    )


def _gate(sql: str) -> LineageRotGate:
    return LineageRotGate({TARGET: sql})


def _mcp_graph(
    claims: dict[str, tuple[str, ...]],
) -> tuple[MCPGraphClient, list[tuple[str, dict[str, object]]]]:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {"entities": [{"urn": TARGET}]}
        if name == "list_schema_fields":
            return {"schema_fields": [{"fieldPath": column} for column in claims]}
        if name == "get_lineage":
            columns = claims[str(arguments["column"])]
            results = (
                [{"entity": {"urn": SOURCE}, "lineageColumns": list(columns)}]
                if columns
                else []
            )
            return {
                "upstreams": {
                    "total": len(results),
                    "returned": len(results),
                    "hasMore": False,
                    "searchResults": results,
                },
                "metadata": {"queryType": "column-level-lineage"},
            }
        raise AssertionError(name)

    return MCPGraphClient(caller), calls


def test_lineage_rot_reports_a_claimed_edge_the_sql_no_longer_produces() -> None:
    evidence = _gate(
        "SELECT c.id AS customer_id FROM analytics.raw_customers AS c"
    ).collect(
        [_asset()], Graph({"customer_id": ("id",), "legacy_email": ("legacy_email",)})
    )

    assert [item.kind for item in evidence] == ["lineage_rot_missing"]
    missing = evidence[0].detail
    assert missing["claimed_edge"]["source_column"] == "legacy_email"
    assert missing["computed_edges"] == []
    assert missing["sql_expression"] is None
    assert missing["confidence"] == "high"


def test_lineage_rot_is_clean_when_catalog_and_sql_agree() -> None:
    evidence = _gate(
        "SELECT c.id AS customer_id FROM analytics.raw_customers AS c"
    ).collect([_asset()], Graph({"customer_id": ("id",)}))

    assert evidence == []


def test_lineage_rot_reports_an_edge_missing_from_the_catalog() -> None:
    evidence = _gate(
        "SELECT c.email AS email FROM analytics.raw_customers AS c"
    ).collect([_asset()], Graph({"email": ()}))

    assert [item.kind for item in evidence] == ["lineage_rot_extra"]
    assert evidence[0].detail["claimed_edge"] is None
    assert evidence[0].detail["computed_edges"] == [
        {"source_dataset": SOURCE, "source_column": "email", "target_column": "email"}
    ]


def test_lineage_rot_never_claims_rot_for_select_star() -> None:
    evidence = _gate("SELECT * FROM analytics.raw_customers").collect(
        [_asset()], Graph({})
    )

    assert [item.kind for item in evidence] == ["lineage_unverifiable"]
    assert "SELECT *" in evidence[0].detail["reason"]


def test_lineage_rot_walks_ctes_and_aliases_to_physical_source_columns() -> None:
    sql = """
    WITH source_rows AS (
      SELECT raw.id AS source_id, raw.email AS source_email
      FROM analytics.raw_customers AS raw
    ), renamed AS (
      SELECT source_id AS customer_id, source_email AS email
      FROM source_rows
    )
    SELECT final.customer_id AS customer_id, final.email AS email
    FROM renamed AS final
    """
    evidence = _gate(sql).collect(
        [_asset()], Graph({"customer_id": ("id",), "email": ("email",)})
    )

    assert evidence == []


def test_lineage_rot_uses_the_official_upstream_mcp_column_contract() -> None:
    graph, calls = _mcp_graph({"customer_id": ("id",)})

    evidence = _gate(
        "SELECT c.id AS customer_id FROM analytics.raw_customers AS c"
    ).collect([_asset()], graph)

    assert evidence == []
    assert calls == [
        ("get_entities", {"urns": [TARGET]}),
        ("list_schema_fields", {"urn": TARGET, "limit": 100}),
        (
            "get_lineage",
            {
                "urn": TARGET,
                "upstream": True,
                "max_hops": 1,
                "max_results": 100,
                "column": "customer_id",
            },
        ),
    ]


def test_lineage_rot_accepts_the_official_empty_upstream_mcp_contract() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def caller(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {"entities": [{"urn": TARGET}]}
        if name == "list_schema_fields":
            return {"schema_fields": [{"fieldPath": "customer_id"}]}
        if name == "get_lineage":
            return {
                "upstreams": {"total": 0},
                "metadata": {"queryType": "column-level-lineage"},
            }
        raise AssertionError(name)

    evidence = _gate(
        "SELECT 1 AS customer_id FROM analytics.raw_customers AS c"
    ).collect([_asset()], MCPGraphClient(caller))

    assert evidence == []
    assert calls == [
        ("get_entities", {"urns": [TARGET]}),
        ("list_schema_fields", {"urn": TARGET, "limit": 100}),
        (
            "get_lineage",
            {
                "urn": TARGET,
                "upstream": True,
                "max_hops": 1,
                "max_results": 100,
                "column": "customer_id",
            },
        ),
    ]


@pytest.mark.parametrize(
    ("sql", "claims", "expected_kind"),
    [
        (
            "SELECT 1 AS legacy_email FROM analytics.raw_customers AS c",
            {"legacy_email": ("legacy_email",)},
            "lineage_rot_missing",
        ),
        (
            "SELECT c.email AS email FROM analytics.raw_customers AS c",
            {"email": ()},
            "lineage_rot_extra",
        ),
    ],
)
def test_lineage_rot_diffs_real_mcp_column_payloads(
    sql: str, claims: dict[str, tuple[str, ...]], expected_kind: str
) -> None:
    graph, _ = _mcp_graph(claims)

    evidence = _gate(sql).collect([_asset()], graph)

    assert [item.kind for item in evidence] == [expected_kind]


@pytest.mark.parametrize(
    "upstreams",
    [
        {
            "total": 1,
            "returned": 0,
            "hasMore": True,
            "searchResults": [],
        },
        {
            "total": 1,
            "returned": 1,
            "hasMore": False,
            "searchResults": [{"entity": {"urn": SOURCE}}],
        },
    ],
)
def test_lineage_rot_fails_closed_on_partial_or_malformed_mcp_lineage(
    upstreams: dict[str, object],
) -> None:
    graph, _ = _mcp_graph({"email": ()})

    def malformed(name: str, arguments: dict[str, object]) -> object:
        if name == "get_lineage":
            return {
                "upstreams": upstreams,
                "metadata": {"queryType": "column-level-lineage"},
            }
        return graph._tool_caller(name, arguments)

    evidence = _gate(
        "SELECT c.email AS email FROM analytics.raw_customers AS c"
    ).collect([_asset()], MCPGraphClient(malformed))

    assert [item.kind for item in evidence] == ["lineage_unverifiable"]
    assert "lineage_rot_missing" not in {item.kind for item in evidence}
    assert "lineage_rot_extra" not in {item.kind for item in evidence}
