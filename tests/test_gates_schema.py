from __future__ import annotations

from pathlib import Path

from sidq.gates.schema import SchemaGate
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import FieldRef, TouchedAsset

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
MISSING = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.missing,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


def test_schema_gate_reports_unknown_field_from_replay_fixture() -> None:
    change = TouchedAsset("urn:target", "model.sql", (), (), (FieldRef(CUSTOMERS, "does_not_exist"),))

    evidence = SchemaGate().collect([change], ReplayGraphClient(FIXTURES))

    assert [(item.kind, item.subject) for item in evidence] == [("unknown_field", f"{CUSTOMERS}#does_not_exist")]


def test_schema_gate_reports_unknown_dataset_and_type_mismatch() -> None:
    change = TouchedAsset(
        "urn:target",
        "model.sql",
        (),
        (),
        (FieldRef(MISSING, "field"), FieldRef(CUSTOMERS, "customer_id::text")),
    )

    evidence = SchemaGate().collect([change], ReplayGraphClient(FIXTURES))

    assert [item.kind for item in evidence] == ["unknown_dataset", "type_mismatch"]
