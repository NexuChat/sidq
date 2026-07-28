from __future__ import annotations

from pathlib import Path

from sidq.gates.blast import BlastRadiusGate
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import TouchedAsset

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


def test_blast_gate_records_column_lineage_paths_from_replay() -> None:
    evidence = BlastRadiusGate().collect([TouchedAsset(ORDERS, "orders.sql", (), ("total",), ())], ReplayGraphClient(FIXTURES))

    detail = evidence[0].detail
    assert evidence[0].kind == "blast_radius"
    assert detail["granularity"] == "column"
    assert detail["downstream_count"] == 1
    assert detail["dashboards"]
    assert detail["critical_assets"]
    assert detail["cross_team_owners"]
    assert detail["paths"]


def test_blast_gate_fails_closed_when_graph_is_unavailable() -> None:
    evidence = BlastRadiusGate().collect([TouchedAsset("urn:unknown", "unknown.sql", (), (), ())], ReplayGraphClient(FIXTURES))

    assert evidence[0].kind == "graph_unavailable"
