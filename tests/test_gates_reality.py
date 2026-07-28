from __future__ import annotations

from pathlib import Path

from sidq.gates.reality import RealityGate
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import TouchedAsset

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


class LiveSource:
    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return DatasetInfo(
            urn,
            (
                SchemaField("id", "integer", False),
                SchemaField("total", "numeric", True),
                SchemaField("status", "text", True),
            ),
        )


def test_reality_gate_reports_catalog_lie_from_replay_fixture() -> None:
    evidence = RealityGate(LiveSource()).collect([TouchedAsset(ORDERS, "orders.sql", (), (), ())], ReplayGraphClient(FIXTURES))

    assert [item.kind for item in evidence] == ["catalog_reality_mismatch"]
    assert evidence[0].detail["missing_in_graph"] == ["status"]
    assert evidence[0].detail["missing_in_source"] == ["old_status"]


def test_reality_gate_fails_closed_when_replay_is_missing() -> None:
    evidence = RealityGate(LiveSource()).collect([TouchedAsset("urn:unknown", "unknown.sql", (), (), ())], ReplayGraphClient(FIXTURES))

    assert evidence[0].kind == "graph_unavailable"
