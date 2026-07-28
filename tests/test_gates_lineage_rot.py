from __future__ import annotations

from sidq.gates.lineage_rot import LineageRotGate
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField
from sidq.models import FieldRef, TouchedAsset

SOURCE = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.raw_customers,PROD)"
TARGET = "urn:li:dataset:(urn:li:dataPlatform:dbt,analytics.customers,PROD)"


class Graph:
    def __init__(self, claims: dict[str, tuple[str, ...]], *, granularity: str = "column") -> None:
        self.claims = claims
        self.granularity = granularity

    def find_dataset(self, name: str) -> str | None:
        return SOURCE if name == "analytics.raw_customers" else None

    def get_dataset(self, urn: str) -> DatasetInfo:
        assert urn == TARGET
        return DatasetInfo(urn, tuple(SchemaField(column, "TEXT", True) for column in self.claims))

    def get_upstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult:
        assert urn == TARGET
        assert depth == 1
        assert column is not None
        return LineageResult(
            urns=(SOURCE,), columns={SOURCE: self.claims.get(column, ())}, granularity=self.granularity
        )


def _asset() -> TouchedAsset:
    return TouchedAsset(
        TARGET,
        "unused.sql",
        (),
        (),
        (FieldRef(SOURCE, "id"), FieldRef(SOURCE, "email"), FieldRef(SOURCE, "legacy_email")),
    )


def _gate(sql: str) -> LineageRotGate:
    return LineageRotGate({TARGET: sql})


def test_lineage_rot_reports_a_claimed_edge_the_sql_no_longer_produces() -> None:
    evidence = _gate("SELECT c.id AS customer_id FROM analytics.raw_customers AS c").collect(
        [_asset()], Graph({"customer_id": ("id",), "legacy_email": ("legacy_email",)})
    )

    assert [item.kind for item in evidence] == ["lineage_rot_missing"]
    missing = evidence[0].detail
    assert missing["claimed_edge"]["source_column"] == "legacy_email"
    assert missing["computed_edges"] == []
    assert missing["sql_expression"] is None
    assert missing["confidence"] == "high"


def test_lineage_rot_is_clean_when_catalog_and_sql_agree() -> None:
    evidence = _gate("SELECT c.id AS customer_id FROM analytics.raw_customers AS c").collect(
        [_asset()], Graph({"customer_id": ("id",)})
    )

    assert evidence == []


def test_lineage_rot_reports_an_edge_missing_from_the_catalog() -> None:
    evidence = _gate("SELECT c.email AS email FROM analytics.raw_customers AS c").collect(
        [_asset()], Graph({"email": ()})
    )

    assert [item.kind for item in evidence] == ["lineage_rot_extra"]
    assert evidence[0].detail["claimed_edge"] is None
    assert evidence[0].detail["computed_edges"] == [{"source_dataset": SOURCE, "source_column": "email", "target_column": "email"}]


def test_lineage_rot_never_claims_rot_for_select_star() -> None:
    evidence = _gate("SELECT * FROM analytics.raw_customers").collect([_asset()], Graph({}))

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
