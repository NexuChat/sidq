"""Source-agnostic conformance checks for constraint reconciliation."""

from __future__ import annotations

import json
from dataclasses import asdict

from sidq.claims.canonical import normalize_constraint
from sidq.claims.models import Claim
from sidq.claims.reconcile import ConstraintReconciler
from sidq.graph.live_source import LiveConstraint

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.raw.orders,PROD)"


class _MemorySource:
    def __init__(self, constraints: tuple[LiveConstraint, ...]) -> None:
        self.constraints = constraints

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        assert urn == URN
        return self.constraints


CORPUS = (
    LiveConstraint(
        "orders_pkey",
        "primary_key",
        ("order_id", "line_no"),
        "PRIMARY KEY (order_id, line_no)",
    ),
    LiveConstraint(
        "orders_customer_placed_key",
        "unique",
        ("customer_id", "placed_on"),
        "UNIQUE (customer_id, placed_on)",
    ),
    LiveConstraint(
        "orders_dates_check",
        "check",
        ("end_date", "start_date"),
        "CHECK (end_date IS NULL OR end_date >= start_date)",
    ),
    LiveConstraint(
        "orders_total_check",
        "check",
        ("total", "subtotal", "tax"),
        "CHECK (total = subtotal + tax)",
    ),
    LiveConstraint(
        "orders_code_length", "check", ("code",), "CHECK (length(code) = 8)"
    ),
    LiveConstraint(
        "orders_code_format", "check", ("code",), "CHECK (code ~ '^[A-Z]{8}$')"
    ),
    LiveConstraint(
        "orders_status_check",
        "check",
        ("status",),
        "CHECK (((status)::text = ANY (ARRAY['a'::text, 'b'::text])))",
    ),
    LiveConstraint(
        "orders_booking_excl",
        "exclude",
        ("room_id", "booking_period"),
        "EXCLUDE USING gist (room_id WITH =, booking_period WITH &&)",
    ),
    LiveConstraint(
        "orders_future_contype",
        "opaque",
        ("tenant_id",),
        "ENFORCE MAGIC (tenant_id) WITH (source = 'future')",
    ),
    LiveConstraint(
        "idx_orders_slug",
        "unique",
        ("slug",),
        "CREATE UNIQUE INDEX idx_orders_slug ON raw.orders USING btree (slug)",
    ),
    LiveConstraint(
        "idx_orders_active_slug",
        "unique",
        ("active_slug",),
        "CREATE UNIQUE INDEX idx_orders_active_slug ON raw.orders (active_slug) WHERE deleted_at IS NULL",
        predicate="(deleted_at IS NULL)",
        is_partial=True,
    ),
)


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


def _truthful_claim(constraint: LiveConstraint) -> Claim:
    if constraint.kind in {"primary_key", "unique"}:
        return _claim("unique", ", ".join(constraint.columns))
    if constraint.kind == "check":
        expression = constraint.definition.removeprefix("CHECK (").removesuffix(")")
        return _claim("expression", constraint.columns[0], expr=expression)
    # Claim has no exclude or opaque vocabulary.  Keeping the raw statement as
    # an expression is honest but intentionally asks the ladder to abstain.
    return _claim("expression", constraint.columns[0], expr=constraint.definition)


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
