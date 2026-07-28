from __future__ import annotations

from pathlib import Path

from sidq.gates.schema import SchemaGate
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import FieldRef, TouchedAsset

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customers,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


def test_schema_gate_reports_unknown_field_from_replay_fixture() -> None:
    change = TouchedAsset("urn:target", "model.sql", (), (), (FieldRef(CUSTOMERS, "email"),))

    evidence = SchemaGate().collect([change], ReplayGraphClient(FIXTURES))

    assert [(item.kind, item.subject) for item in evidence] == [("unknown_field", f"{CUSTOMERS}#email")]


def test_schema_gate_reports_unknown_dataset_and_type_mismatch() -> None:
    change = TouchedAsset(
        "urn:target",
        "model.sql",
        (),
        (),
        (FieldRef("urn:missing", "field"), FieldRef(CUSTOMERS, "customer_id::text")),
    )

    evidence = SchemaGate().collect([change], ReplayGraphClient(FIXTURES))

    assert [item.kind for item in evidence] == ["type_mismatch", "unknown_dataset"]
