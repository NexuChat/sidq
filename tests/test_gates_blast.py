from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from sidq.gates.blast import BlastRadiusGate
from sidq.graph.client import DatasetInfo, LineageResult, _owners
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import TouchedAsset

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


class DirectedReplayGraph(ReplayGraphClient):
    """Expose the downstream relations which the replay verifies."""

    def __init__(self, fixture_dir: Path) -> None:
        super().__init__(fixture_dir)
        self.downstream_edges: set[tuple[str, str]] = set()

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ):
        paths = super().paths_between(a, b, source_column, target_column)
        if paths:
            for path in paths:
                self.downstream_edges.update(pairwise(path.urns))
        return paths


def test_blast_gate_emits_a_directed_column_path_to_dashboard() -> None:
    graph = DirectedReplayGraph(FIXTURES)
    evidence = BlastRadiusGate().collect(
        [TouchedAsset(CUSTOMERS, "customers.sql", (), ("cust_email",), ())],
        graph,
    )

    detail = evidence[0].detail
    assert evidence[0].kind == "blast_radius"
    assert detail["granularity"] == "column"
    assert detail["downstream_count"] == 16
    assert detail["dashboards"] == ["urn:li:dashboard:(looker,b2fd91.dashboards.53)"]
    assert detail["pii_tags"] == ["urn:li:tag:b2fd91.PII_Data"]
    changed_field = f"urn:li:schemaField:({CUSTOMERS},cust_email)"
    dashboard = "urn:li:dashboard:(looker,b2fd91.dashboards.53)"
    path = next(item for item in detail["paths"] if item["target"] == dashboard)
    hops = list(path["hops"].values())

    assert path["source"] == changed_field
    assert hops[0]["from"] == path["source"]
    assert hops[-1]["to"] == path["target"]
    assert all(left["to"] == right["from"] for left, right in pairwise(hops))
    handoff = (
        "urn:li:schemaField:(urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD),order_details.cust_email)",
        "urn:li:dataset:(urn:li:dataPlatform:looker,b2fd91.order-entry.explore.order_details,PROD)",
    )
    assert path["granularity"] == "column"
    assert all(
        (hop["from"], hop["to"]) in graph.downstream_edges
        for hop in hops
        if (hop["from"], hop["to"]) != handoff
    )
    assert handoff in [(hop["from"], hop["to"]) for hop in hops]
    assert "chart and dashboard hops are entity-level" in path["note"]

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


def test_an_unreadable_downstream_asset_is_named_in_the_blast_detail() -> None:
    """A failed downstream read must be auditable, never silently dropped.

    `critical_assets` and `cross_team_owners` are what `critical_downstream`
    blocks on, so a partial read that leaves them empty could weaken a blocking
    verdict with no trace of why.
    """

    class PartiallyReadableGraph:
        def get_dataset(self, urn: str) -> DatasetInfo | None:
            if urn.startswith("urn:li:chart:"):
                raise RuntimeError("charts are not datasets in this graph")
            return DatasetInfo(urn, (), (), (), ("urn:li:corpuser:owner",))

        def get_downstream(
            self, urn: str, depth: int, column: str | None = None
        ) -> LineageResult:
            return LineageResult(
                urns=("urn:li:chart:(looker,dash.1)",),
                entity_types={"urn:li:chart:(looker,dash.1)": "chart"},
                granularity="column" if column else "table",
            )

        def paths_between(self, *args: object, **kwargs: object) -> list:
            return []

    evidence = BlastRadiusGate(depth=3).collect(
        [
            TouchedAsset(
                "urn:li:dataset:(urn:li:dataPlatform:dbt,db.t,PROD)", "", (), ("x",), ()
            )
        ],
        PartiallyReadableGraph(),
    )

    radius = next(item for item in evidence if item.kind == "blast_radius")
    assert radius.detail["unreadable_assets"] == ["urn:li:chart:(looker,dash.1)"]
    assert radius.detail["cross_team_owners"] == []


def test_a_fully_readable_radius_records_no_unreadable_assets() -> None:
    class ReadableGraph:
        def get_dataset(self, urn: str) -> DatasetInfo | None:
            return DatasetInfo(urn, (), (), (), ())

        def get_downstream(
            self, urn: str, depth: int, column: str | None = None
        ) -> LineageResult:
            return LineageResult(
                urns=("urn:li:dataset:(urn:li:dataPlatform:dbt,db.d,PROD)",)
            )

        def paths_between(self, *args: object, **kwargs: object) -> list:
            return []

    evidence = BlastRadiusGate(depth=3).collect(
        [
            TouchedAsset(
                "urn:li:dataset:(urn:li:dataPlatform:dbt,db.t,PROD)", "", (), ("x",), ()
            )
        ],
        ReadableGraph(),
    )

    radius = next(item for item in evidence if item.kind == "blast_radius")
    assert radius.detail["unreadable_assets"] == []
