"""Conservatively reconcile catalog claims with live database constraints."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from sidq.graph.live_source import LiveConstraint
from sidq.models import Evidence

from .canonical import NormalizedConstraint, Shape, equivalent, normalize_constraint
from .models import Claim


class ConstraintSource(Protocol):
    """The read-only live-source capability needed by reconciliation."""

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]: ...


@dataclass(frozen=True, slots=True)
class _CatalogConstraint:
    claim: Claim
    normalized: NormalizedConstraint


@dataclass(frozen=True, slots=True)
class _LiveConstraint:
    live: LiveConstraint
    normalized: NormalizedConstraint


class ConstraintReconciler:
    """Compare catalog claims to source enforcement without over-claiming."""

    def __init__(self, source: ConstraintSource) -> None:
        self._source = source

    def reconcile(self, urn: str, catalog_claims: Sequence[Claim]) -> list[Evidence]:
        """Return one evidence record per live constraint, plus catalog-only claims.

        A record is deliberately emitted for every source item before any attempt
        to recognise its SQL.  This makes coverage a structural property rather
        than a consequence of the currently supported predicate vocabulary.
        """
        live_constraints = tuple(
            _LiveConstraint(
                item,
                normalize_constraint(
                    item.kind,
                    item.columns,
                    item.definition,
                    is_partial=item.is_partial,
                    partial_predicate=item.predicate,
                ),
            )
            for item in self._source.get_constraints(urn)
        )
        catalog_constraints = tuple(_normalize_claim(claim) for claim in catalog_claims)
        records: list[Evidence] = []
        addressed_catalog: set[Claim] = set()

        for database in live_constraints:
            candidates = _counterparts(database.normalized, catalog_constraints)
            evidence, addressed = _reconcile_live(urn, database, candidates)
            records.append(evidence)
            addressed_catalog.update(addressed)

        for catalog in catalog_constraints:
            if catalog.claim in addressed_catalog:
                continue
            records.append(_reconcile_catalog_only(urn, catalog, live_constraints))

        return sorted(records, key=_evidence_key)


def reconcile_constraints(
    urn: str, catalog_claims: Sequence[Claim], source: ConstraintSource
) -> list[Evidence]:
    """Convenience wrapper for one-off, read-only reconciliation."""
    return ConstraintReconciler(source).reconcile(urn, catalog_claims)


def _normalize_claim(claim: Claim) -> _CatalogConstraint:
    kind, raw_ddl = _claim_sql(claim)
    initial = normalize_constraint(kind, (claim.column,), raw_ddl)
    columns = _claim_columns(claim, initial)
    normalized = normalize_constraint(kind, columns, raw_ddl)
    return _CatalogConstraint(claim, normalized)


def _claim_sql(claim: Claim) -> tuple[str, str]:
    if claim.type == "expression":
        return "check", f"CHECK ({claim.expr or ''})"
    if claim.type == "accepted_values":
        values = ", ".join(_sql_literal(value) for value in claim.values or ())
        return "check", f"CHECK ({claim.column} IN ({values}))"
    if claim.type == "not_null":
        return "not_null", f"{claim.column} IS NOT NULL"
    if claim.type == "unique":
        return "unique", f"UNIQUE ({claim.column})"
    target = (claim.expr or "").rsplit(".", 1)
    if len(target) == 2 and all(target):
        return (
            "foreign_key",
            f"FOREIGN KEY ({claim.column}) REFERENCES {target[0]}({target[1]})",
        )
    return "foreign_key", f"FOREIGN KEY ({claim.column})"


def _claim_columns(claim: Claim, normalized: NormalizedConstraint) -> tuple[str, ...]:
    if claim.type != "expression" or normalized.shape is None:
        return (claim.column,)
    return (
        normalized.shape.columns
        if len(normalized.shape.columns) > 1
        else (claim.column,)
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _counterparts(
    database: NormalizedConstraint,
    catalog: Sequence[_CatalogConstraint],
) -> tuple[_CatalogConstraint, ...]:
    same_subject = tuple(
        item
        for item in catalog
        if _same_subject(database, item.normalized)
        and _same_kind_family(database, item.normalized)
    )
    if same_subject:
        return same_subject
    # An identical predicate is positive proof even when a legacy single-column
    # Claim cannot express the source's declared composite subject.
    return tuple(
        item
        for item in catalog
        if _same_kind_family(database, item.normalized)
        and _equivalence_tier(database, item.normalized) is not None
    ) or tuple(
        item
        for item in catalog
        if _subjects_overlap(database, item.normalized)
        and (
            database.kind == "opaque"
            or database.canonical is None
            or item.normalized.canonical is None
        )
    )


def _same_subject(left: NormalizedConstraint, right: NormalizedConstraint) -> bool:
    return tuple(column.casefold() for column in left.columns) == tuple(
        column.casefold() for column in right.columns
    )


def _subjects_overlap(left: NormalizedConstraint, right: NormalizedConstraint) -> bool:
    return not {column.casefold() for column in left.columns}.isdisjoint(
        column.casefold() for column in right.columns
    )


def _same_kind_family(left: NormalizedConstraint, right: NormalizedConstraint) -> bool:
    return _kind_family(left) == _kind_family(right)


def _kind_family(constraint: NormalizedConstraint) -> str:
    if constraint.kind in {"unique", "primary_key"}:
        return "unique"
    if (
        constraint.kind == "check"
        and constraint.shape is not None
        and constraint.shape.name == "not_null"
    ):
        return "not_null"
    return constraint.kind


def _reconcile_live(
    urn: str,
    database: _LiveConstraint,
    candidates: Sequence[_CatalogConstraint],
) -> tuple[Evidence, set[Claim]]:
    normalized = database.normalized
    if normalized.is_partial:
        return (
            _live_evidence(
                "constraint_unverifiable",
                urn,
                database,
                candidates,
                tier="T4",
                reason="partial_constraint_cannot_prove_unconditional_claim",
            ),
            {item.claim for item in candidates},
        )
    if not candidates:
        return (
            _live_evidence(
                "constraint_missing_in_catalog",
                urn,
                database,
                (),
                tier="catalog_silent",
                reason="no_catalog_counterpart_for_enforced_constraint",
            ),
            set(),
        )

    for candidate in candidates:
        tier = _equivalence_tier(normalized, candidate.normalized)
        if tier is not None:
            return (
                _live_evidence(
                    "constraint_confirmed",
                    urn,
                    database,
                    (candidate,),
                    tier=tier,
                    reason="catalog_claim_matches_enforced_constraint",
                ),
                {candidate.claim},
            )

    conflicts = tuple(
        item for item in candidates if _positive_conflict(normalized, item.normalized)
    )
    if conflicts:
        return (
            _live_evidence(
                "constraint_contradicts_catalog",
                urn,
                database,
                conflicts,
                tier="T3",
                reason="recognised_constraint_parameters_conflict",
            ),
            {item.claim for item in candidates},
        )

    reason = (
        "database_constraint_is_opaque_or_uncanonical"
        if normalized.kind == "opaque" or normalized.canonical is None
        else "comparison_is_outside_decidable_equivalence_ladder"
    )
    return (
        _live_evidence(
            "constraint_unverifiable",
            urn,
            database,
            candidates,
            tier="T4",
            reason=reason,
        ),
        {item.claim for item in candidates},
    )


def _equivalence_tier(
    database: NormalizedConstraint, catalog: NormalizedConstraint
) -> str | None:
    if not _same_kind_family(database, catalog):
        return None
    matched, result = equivalent(_comparison_sql(database), _comparison_sql(catalog))
    if not matched:
        return None
    return {"identical": "T0", "canonical": "T1", "rewrite": "T2"}[result]


def _comparison_sql(constraint: NormalizedConstraint) -> str:
    family = _kind_family(constraint)
    if family == "unique":
        return f"UNIQUE ({', '.join(constraint.columns)})"
    if family == "not_null" and len(constraint.columns) == 1:
        return f"{constraint.columns[0]} IS NOT NULL"
    return constraint.raw_ddl


def _positive_conflict(
    database: NormalizedConstraint, catalog: NormalizedConstraint
) -> bool:
    if (
        not _same_subject(database, catalog)
        or not _same_kind_family(database, catalog)
        or database.canonical is None
        or catalog.canonical is None
        or database.shape is None
        or catalog.shape is None
        or database.shape.name != catalog.shape.name
    ):
        return False
    return _shape_parameters_conflict(database.shape, catalog.shape)


def _shape_parameters_conflict(left: Shape, right: Shape) -> bool:
    if left.name == "accepted_values":
        return left.column == right.column and left.values != right.values
    if left.name == "range":
        return left.column == right.column and (
            left.lower,
            left.upper,
            left.lower_inclusive,
            left.upper_inclusive,
        ) != (right.lower, right.upper, right.lower_inclusive, right.upper_inclusive)
    if left.name == "comparison":
        return left.column == right.column and (left.operator, left.value) != (
            right.operator,
            right.value,
        )
    if left.name == "pattern":
        return left.column == right.column and left.pattern != right.pattern
    if left.name == "foreign_key":
        return (left.target_table, left.target_columns) != (
            right.target_table,
            right.target_columns,
        )
    return False


def _reconcile_catalog_only(
    urn: str,
    catalog: _CatalogConstraint,
    live_constraints: Sequence[_LiveConstraint],
) -> Evidence:
    blocker = _undetermined_database_constraint(catalog.normalized, live_constraints)
    if blocker is not None:
        return _catalog_evidence(
            "constraint_unverifiable",
            urn,
            catalog,
            tier="T4",
            reason="database_constraint_on_subject_is_opaque_or_undetermined",
            blocking_constraint=blocker,
        )
    if catalog.normalized.canonical is None:
        return _catalog_evidence(
            "constraint_unverifiable",
            urn,
            catalog,
            tier="T4",
            reason="catalog_claim_is_not_fully_canonical",
        )
    return _catalog_evidence(
        "constraint_contradicts_catalog",
        urn,
        catalog,
        tier="T3",
        reason="catalog_claim_has_no_matching_enforced_constraint",
    )


def _undetermined_database_constraint(
    catalog: NormalizedConstraint, live_constraints: Sequence[_LiveConstraint]
) -> _LiveConstraint | None:
    catalog_columns = {column.casefold() for column in catalog.columns}
    for database in live_constraints:
        database_columns = {column.casefold() for column in database.normalized.columns}
        if catalog_columns.isdisjoint(database_columns):
            continue
        if (
            database.normalized.is_partial
            or database.normalized.kind == "opaque"
            or database.normalized.canonical is None
        ):
            return database
    return None


def _live_evidence(
    kind: str,
    urn: str,
    database: _LiveConstraint,
    catalog: Sequence[_CatalogConstraint],
    *,
    tier: str,
    reason: str,
) -> Evidence:
    return Evidence(
        kind,
        _subject(urn, database.normalized.columns),
        _detail(
            database.live,
            database.normalized,
            catalog,
            tier=tier,
            reason=reason,
        ),
    )


def _catalog_evidence(
    kind: str,
    urn: str,
    catalog: _CatalogConstraint,
    *,
    tier: str,
    reason: str,
    blocking_constraint: _LiveConstraint | None = None,
) -> Evidence:
    normalized = (
        blocking_constraint.normalized if blocking_constraint else catalog.normalized
    )
    detail = {
        "constraint_name": blocking_constraint.live.name
        if blocking_constraint
        else None,
        "constraint_kind": blocking_constraint.live.kind
        if blocking_constraint
        else None,
        "raw_ddl": blocking_constraint.live.definition if blocking_constraint else None,
        "fingerprint": normalized.fingerprint,
        "canonical": normalized.canonical,
        "shape": _shape_detail(normalized.shape),
        "tier": tier,
        "reason": reason,
        "database_constraint": _constraint_detail(blocking_constraint.normalized)
        if blocking_constraint
        else None,
        "catalog_claims": [_claim_detail(catalog.claim)],
    }
    return Evidence(kind, _subject(urn, catalog.normalized.columns), detail)


def _detail(
    constraint: LiveConstraint,
    normalized: NormalizedConstraint,
    catalog: Sequence[_CatalogConstraint],
    *,
    tier: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "constraint_name": constraint.name,
        "constraint_kind": constraint.kind,
        "raw_ddl": constraint.definition,
        "fingerprint": normalized.fingerprint,
        "canonical": normalized.canonical,
        "shape": _shape_detail(normalized.shape),
        "tier": tier,
        "reason": reason,
        "database_constraint": _constraint_detail(normalized),
        "catalog_claims": [_claim_detail(item.claim) for item in catalog],
    }


def _constraint_detail(constraint: NormalizedConstraint) -> dict[str, Any]:
    return {
        "kind": constraint.kind,
        "columns": list(constraint.columns),
        "canonical": constraint.canonical,
        "fingerprint": constraint.fingerprint,
        "shape": _shape_detail(constraint.shape),
        "is_partial": constraint.is_partial,
        "partial_predicate": constraint.partial_predicate,
    }


def _shape_detail(shape: Shape | None) -> dict[str, Any] | None:
    return asdict(shape) if shape is not None else None


def _claim_detail(claim: Claim) -> dict[str, Any]:
    detail = asdict(claim)
    if detail["values"] is not None:
        detail["values"] = list(detail["values"])
    return detail


def _subject(urn: str, columns: tuple[str, ...]) -> str:
    if len(columns) == 1:
        return f"{urn}#{columns[0]}"
    return f"{urn}#({','.join(columns)})"


def _evidence_key(item: Evidence) -> tuple[str, str, str, str]:
    return (
        item.subject,
        item.kind,
        str(item.detail["constraint_name"] or ""),
        str(item.detail["fingerprint"]),
    )
