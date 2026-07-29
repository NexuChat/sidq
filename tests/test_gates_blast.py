from __future__ import annotations

from pathlib import Path

from sidq.gates.blast import BlastRadiusGate
from sidq.graph.client import DatasetInfo, LineageResult, _owners
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import TouchedAsset

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


def test_blast_gate_records_column_lineage_paths_from_replay() -> None:
    evidence = BlastRadiusGate().collect(
        [TouchedAsset(CUSTOMERS, "customers.sql", (), ("cust_email",), ())],
        ReplayGraphClient(FIXTURES),
    )

    detail = evidence[0].detail
    assert evidence[0].kind == "blast_radius"
    assert detail["granularity"] == "column"
    assert detail["downstream_count"] == 16
    assert detail["dashboards"] == ["urn:li:dashboard:(looker,b2fd91.dashboards.53)"]
    assert detail["pii_tags"] == ["urn:li:tag:b2fd91.PII_Data"]
    assert any(
        "b2fd91.ORDER_ENTRY_DB.analytics.order_details" in urn
        for path in detail["paths"]
        for hop in path["hops"].values()
        for urn in hop.values()
    )
    pii = next(item for item in evidence if item.kind == "pii_exposure")
    assert pii.detail["pii_tags"] == ["urn:li:tag:b2fd91.PII_Data"]


def test_blast_gate_fails_closed_when_graph_is_unavailable() -> None:
    evidence = BlastRadiusGate().collect(
        [TouchedAsset("urn:unknown", "unknown.sql", (), (), ())],
        ReplayGraphClient(FIXTURES),
    )

    assert evidence[0].kind == "graph_unavailable"


def test_blast_gate_reads_owner_and_criticality_metadata_even_when_lineage_has_inline_tags() -> (
    None
):
    source = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.source,PROD)"
    downstream = "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.downstream,PROD)"

    class Graph:
        def get_downstream(
            self, urn: str, depth: int, column: str | None = None
        ) -> LineageResult:
            return LineageResult(
                urns=(downstream,),
                entity_types={downstream: "dataset"},
                tags={downstream: ("urn:li:tag:critical",)},
                granularity="table",
            )

        def get_dataset(self, urn: str) -> DatasetInfo:
            if urn == source:
                return DatasetInfo(source, owners=("urn:li:corpGroup:platform",))
            return DatasetInfo(
                downstream,
                tags=("urn:li:tag:critical",),
                owners=("urn:li:corpGroup:finance",),
            )

        def paths_between(self, *args, **kwargs):
            return []

    detail = (
        BlastRadiusGate()
        .collect([TouchedAsset(source, "source.sql", (), (), ())], Graph())[0]
        .detail
    )

    assert detail["critical_assets"] == [downstream]
    assert detail["cross_team_owners"] == ["urn:li:corpGroup:finance"]


def test_owner_parser_excludes_ownership_type_urns() -> None:
    owners = _owners(
        {
            "ownership": [
                {
                    "owner": {"urn": "urn:li:corpuser:alice"},
                    "type": {"urn": "urn:li:ownershipType:__system__technical_owner"},
                }
            ]
        }
    )

    assert owners == ["urn:li:corpuser:alice"]
