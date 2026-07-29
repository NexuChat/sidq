"""Source-agnostic conformance checks for constraint reconciliation."""

from __future__ import annotations

import json
from dataclasses import asdict

from scripts import measure_reconcile
from scripts.measure_reconcile import CORPUS, URN
from scripts.measure_reconcile import truthful_claim as _truthful_claim
from sidq.claims.canonical import normalize_constraint
from sidq.claims.models import Claim
from sidq.claims.reconcile import ConstraintReconciler
from sidq.graph.live_source import LiveConstraint


class _MemorySource:
    def __init__(self, constraints: tuple[LiveConstraint, ...]) -> None:
        self.constraints = constraints

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        assert urn == URN
        return self.constraints


def _claim(
    claim_type: str,
    column: str,
    *,
    values: tuple[str, ...] | None = None,
    expr: str | None = None,
) -> Claim:
    return Claim(
        claim_type,  # type: ignore[arg-type] -- fixtures deliberately use Claim's public vocabulary
        column,
        values=values,
        expr=expr,
        source_sentence="truthful in-memory catalog fixture",
        confidence=1.0,
    )


def _records(claims: tuple[Claim, ...] = ()):
    return ConstraintReconciler(_MemorySource(CORPUS)).reconcile(URN, claims)


def test_coverage_is_one_record_per_enumerated_constraint() -> None:
    source = _MemorySource(CORPUS)
    records = ConstraintReconciler(source).reconcile(URN, ())

    assert len(records) == len(source.get_constraints(URN))
    assert all(
        {
            "constraint_name",
            "constraint_kind",
            "raw_ddl",
            "fingerprint",
            "canonical",
            "shape",
            "tier",
            "reason",
        }
        <= record.detail.keys()
        for record in records
    )


def test_truthful_catalog_never_produces_a_false_accusation() -> None:
    records = _records(tuple(_truthful_claim(constraint) for constraint in CORPUS))

    assert len(records) == len(CORPUS)
    assert {record.kind for record in records} <= {
        "constraint_confirmed",
        "constraint_unverifiable",
    }
    assert "constraint_confirmed" in {record.kind for record in records}


def test_reconciliation_is_byte_deterministic() -> None:
    claims = tuple(_truthful_claim(constraint) for constraint in CORPUS)

    first = json.dumps([asdict(record) for record in _records(claims)], sort_keys=True)
    second = json.dumps([asdict(record) for record in _records(claims)], sort_keys=True)

    assert first == second


def test_fingerprints_are_stable_for_equivalent_ddl_and_distinct_otherwise() -> None:
    first = normalize_constraint("check", ("status",), "CHECK (status IN ('a', 'b'))")
    same = normalize_constraint(
        "check",
        ("status",),
        "CHECK (((status)::text = ANY (ARRAY['b'::text, 'a'::text])))",
    )
    different = normalize_constraint(
        "check", ("status",), "CHECK (status IN ('a', 'c'))"
    )

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != different.fingerprint


def test_abstentions_are_reasoned_and_never_contradictions() -> None:
    records = _records(tuple(_truthful_claim(constraint) for constraint in CORPUS))
    abstentions = [
        record for record in records if record.kind == "constraint_unverifiable"
    ]

    assert abstentions
    assert all(record.detail["reason"] for record in abstentions)
    assert all(
        record.kind != "constraint_contradicts_catalog" for record in abstentions
    )


def test_opaque_constraint_is_reported_with_verbatim_ddl() -> None:
    records = _records(tuple(_truthful_claim(constraint) for constraint in CORPUS))
    opaque = next(
        record
        for record in records
        if record.detail["constraint_name"] == "orders_future_contype"
    )

    assert opaque.kind == "constraint_unverifiable"
    assert (
        opaque.detail["raw_ddl"] == "ENFORCE MAGIC (tenant_id) WITH (source = 'future')"
    )


def test_recognised_parameter_conflict_is_still_detected() -> None:
    source = _MemorySource(
        (
            LiveConstraint(
                "orders_status_check",
                "check",
                ("status",),
                "CHECK (status IN ('a', 'c'))",
            ),
        )
    )
    catalog = (_claim("accepted_values", "status", values=("a", "b")),)

    records = ConstraintReconciler(source).reconcile(URN, catalog)

    assert [record.kind for record in records] == ["constraint_contradicts_catalog"]
    assert records[0].detail["tier"] == "T3"


def test_published_coverage_document_matches_the_engine() -> None:
    """`docs/RECONCILE-COVERAGE.md` is a claim; a stale claim is a false one.

    RECONCILE-SPEC.md §6 publishes coverage and precision. If the ladder changes
    and the document is not regenerated, the repository would be advertising a
    number the engine no longer produces.
    """
    assert measure_reconcile.main.__module__ == "scripts.measure_reconcile"
    rendered = measure_reconcile._render(measure_reconcile.measure())
    committed = measure_reconcile.DOCUMENT.read_text(encoding="utf-8")

    assert committed == rendered, (
        "docs/RECONCILE-COVERAGE.md is stale; rerun scripts/measure_reconcile.py"
    )


def test_the_measured_corpus_is_the_conformance_corpus() -> None:
    """One corpus, imported in both directions, so the two can never disagree."""
    assert measure_reconcile.CORPUS is CORPUS
    assert len(CORPUS) == measure_reconcile.measure()["enumerated"]


def test_a_truthful_catalog_measures_zero_false_accusations() -> None:
    assert measure_reconcile.measure()["false_accusations"] == 0
