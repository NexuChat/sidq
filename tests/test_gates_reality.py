from __future__ import annotations

from pathlib import Path

from sidq.gates.reality import RealityGate
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import TouchedAsset

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


class LiveSource:
    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return DatasetInfo(
            urn,
            (
                SchemaField("customer_id", "NUMBER", False),
                SchemaField("cust_email", "TEXT", False),
                SchemaField("unexpected_live_column", "TEXT", True),
            ),
        )


def test_reality_gate_reports_catalog_lie_from_replay_fixture() -> None:
    evidence = RealityGate(LiveSource()).collect(
        [TouchedAsset(CUSTOMERS, "customers.sql", (), (), ())],
        ReplayGraphClient(FIXTURES),
    )

    assert [item.kind for item in evidence] == ["catalog_reality_mismatch"]
    assert evidence[0].detail["missing_in_graph"] == ["unexpected_live_column"]
    assert "cust_first_name" in evidence[0].detail["missing_in_source"]


def test_reality_gate_fails_closed_when_replay_is_missing() -> None:
    evidence = RealityGate(LiveSource()).collect(
        [TouchedAsset("urn:unknown", "unknown.sql", (), (), ())],
        ReplayGraphClient(FIXTURES),
    )

    assert evidence[0].kind == "graph_unavailable"
