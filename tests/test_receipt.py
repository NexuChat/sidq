from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidq.models import Evidence, Finding, Verdict
from sidq.receipt.bootstrap import PROPERTY_DEFINITIONS, ensure_sidq_properties
from sidq.receipt.build import build_receipt
from sidq.receipt.read import get_verification_status
from sidq.receipt.write import write_receipt

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.test,DEV)"


def _verdict(decision: str = "PASS") -> Verdict:
    finding = Finding(
        "schema.required",
        "block",
        "A required field is missing.",
        (Evidence("schema", URN, {"field": "email"}),),
    )
    return Verdict(
        decision,
        "MISSING_FIELD" if decision == "BLOCK" else None,
        (finding,),
        (),
        "a" * 40,
        "sha256:policy",
    )


def test_build_receipt_is_deterministic_and_records_block() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    assert receipt.verdict == "BLOCK"
    assert receipt.rules_fired == ("schema.required",)
    assert receipt.checked_at == "2026-08-02T11:04:00Z"
    assert receipt.structured_property_values()[
        "urn:li:structuredProperty:sidq.reason_code"
    ] == ["MISSING_FIELD"]


def test_write_uses_only_the_three_official_mcp_mutation_tools() -> None:
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        return {"success": True, "urn": "urn:li:document:sidq-receipt"}

    written = write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "save_document",
        "add_structured_properties",
        "add_tags",
    ]
    assert calls[1][1]["property_values"][
        "urn:li:structuredProperty:sidq.evidence_url"
    ] == ["urn:li:document:sidq-receipt"]
    assert calls[2][1]["tag_urns"] == ["urn:li:tag:sidq:verified"]
    assert written["receipt"]["evidence_url"] == "urn:li:document:sidq-receipt"


def test_block_receipt_is_written_and_uses_the_blocked_badge() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        return {"success": True, "urn": "urn:li:document:sidq-blocked"}

    write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "save_document",
        "add_structured_properties",
        "add_tags",
    ]
    assert calls[-1][1]["tag_urns"] == ["urn:li:tag:sidq:blocked"]


def test_read_computes_schema_policy_and_age_staleness() -> None:
    entity = {
        "urn": URN,
        "schemaMetadata": {"lastModified": {"time": 1785668700000}},
        "structuredProperties": {
            "properties": [
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.verdict"
                    },
                    "values": [{"stringValue": "PASS"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.reason_code"
                    },
                    "values": [{"stringValue": ""}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.commit_sha"
                    },
                    "values": [{"stringValue": "abc"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.checked_at"
                    },
                    "values": [{"stringValue": "2026-08-02T11:04:00Z"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.policy_hash"
                    },
                    "values": [{"stringValue": "sha256:old"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.rules_fired"
                    },
                    "values": [{"stringValue": "schema.required"}],
                },
            ]
        },
    }
    caller = lambda name, arguments: [entity]

    schema_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:old",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    assert schema_stale["stale"] is True
    assert (
        schema_stale["stale_reason"]
        == "asset schema changed after the last verification"
    )

    entity["schemaMetadata"]["lastModified"]["time"] = 1785668400000
    policy_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:new",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    assert (
        policy_stale["stale_reason"]
        == "policy hash changed since the last verification"
    )

    age_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:old",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        max_age=timedelta(days=7),
    )
    assert age_stale["stale_reason"] == "receipt exceeded the maximum verification age"


def test_bootstrap_is_idempotent_with_a_graph_double() -> None:
    class Graph:
        def __init__(self) -> None:
            self.aspects: dict[str, object] = {}

        def get_aspect(self, urn: str, aspect_type: object) -> object | None:
            return self.aspects.get(urn)

        def emit_mcp(self, mcp: object) -> None:
            self.aspects[mcp.entityUrn] = mcp.aspect

    graph = Graph()
    first = ensure_sidq_properties(graph)
    second = ensure_sidq_properties(graph)

    assert len(first["created"]) == len(PROPERTY_DEFINITIONS) + 2
    assert not second["created"]
