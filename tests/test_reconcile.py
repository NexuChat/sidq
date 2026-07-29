from __future__ import annotations

from sidq.claims.models import Claim
from sidq.claims.reconcile import ConstraintReconciler
from sidq.graph.live_source import LiveConstraint
from sidq.policy.engine import PolicyEngine

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.raw.orders,PROD)"


class _Source:
    def __init__(self, *constraints: LiveConstraint) -> None:
        self.constraints = constraints
        self.urn: str | None = None

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        self.urn = urn
        return self.constraints


def _claim(
    claim_type: str,
    column: str,
    *,
    values: tuple[str, ...] | None = None,
    expr: str | None = None,
) -> Claim:
    return Claim(
        claim_type,  # type: ignore[arg-type] -- fixtures exercise valid vocabulary
        column,
        values=values,
        expr=expr,
        source_sentence="catalog fixture",
        confidence=1.0,
    )


def test_database_constraint_missing_from_catalog_includes_raw_ddl() -> None:
    source = _Source(
        LiveConstraint(
            "orders_status_check",
            "check",
            ("status",),
            "CHECK (status IN ('pending', 'paid'))",
        )
    )

    evidence = ConstraintReconciler(source).reconcile(URN, ())

    assert source.urn == URN
    assert [(item.kind, item.subject) for item in evidence] == [
        ("constraint_missing_in_catalog", f"{URN}#status")
    ]
    assert evidence[0].detail["raw_ddl"] == "CHECK (status IN ('pending', 'paid'))"
    assert evidence[0].detail["shape"]["values"] == ("paid", "pending")
    assert evidence[0].detail["catalog_claims"] == []
    assert evidence[0].detail["tier"] == "catalog_silent"


def test_different_catalog_values_are_a_warning_not_a_block() -> None:
    source = _Source(
        LiveConstraint(
            "orders_status_check",
            "check",
            ("status",),
            "CHECK (status = ANY (ARRAY['pending'::text, 'paid'::text]))",
        )
    )

    evidence = ConstraintReconciler(source).reconcile(
        URN, (_claim("accepted_values", "status", values=("pending", "refunded")),)
    )

    assert [item.kind for item in evidence] == ["constraint_contradicts_catalog"]
    assert evidence[0].detail["raw_ddl"].startswith("CHECK (status = ANY")
    assert evidence[0].detail["catalog_claims"][0]["values"] == [
        "pending",
        "refunded",
    ]
    assert PolicyEngine().decide(evidence).decision == "WARN"


def test_unrecognised_check_abstains_instead_of_claiming_catalog_contradiction() -> (
    None
):
    source = _Source(
        LiveConstraint(
            "orders_status_lower_check",
            "check",
            ("status",),
            "CHECK (lower(status) IN ('pending', 'paid'))",
        )
    )

    evidence = ConstraintReconciler(source).reconcile(
        URN, (_claim("accepted_values", "status", values=("pending", "paid")),)
    )

    assert [item.kind for item in evidence] == ["constraint_unverifiable"]
    assert (
        evidence[0].detail["raw_ddl"] == "CHECK (lower(status) IN ('pending', 'paid'))"
    )
    assert evidence[0].detail["tier"] == "T4"
    assert (
        evidence[0].detail["reason"]
        == "comparison_is_outside_decidable_equivalence_ladder"
    )


def test_maps_not_null_unique_primary_key_foreign_key_and_simple_expression() -> None:
    source = _Source(
        LiveConstraint(
            "orders_status_not_null", "not_null", ("status",), "status NOT NULL"
        ),
        LiveConstraint(
            "orders_reference_unique", "unique", ("reference",), "UNIQUE (reference)"
        ),
        LiveConstraint(
            "orders_pkey", "primary_key", ("order_id",), "PRIMARY KEY (order_id)"
        ),
        LiveConstraint(
            "orders_customer_id_fkey",
            "foreign_key",
            ("customer_id",),
            "FOREIGN KEY (customer_id) REFERENCES raw.customers(customer_id)",
            "raw",
            "customers",
            ("customer_id",),
        ),
        LiveConstraint(
            "orders_total_check",
            "check",
            ("order_total",),
            "CHECK (order_total >= 0::numeric)",
        ),
    )
    catalog = (
        _claim("not_null", "status"),
        _claim("unique", "reference"),
        _claim("unique", "order_id"),
        _claim("relationships", "customer_id", expr="raw.customers.customer_id"),
        _claim("expression", "order_total", expr="order_total >= 0"),
    )

    evidence = ConstraintReconciler(source).reconcile(URN, catalog)

    assert len(evidence) == len(source.constraints)
    assert {item.kind for item in evidence} == {"constraint_confirmed"}
    assert {item.detail["tier"] for item in evidence} <= {"T0", "T1", "T2"}
