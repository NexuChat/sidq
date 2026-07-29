#!/usr/bin/env python3
"""Measure constraint-reconciliation coverage and publish `docs/RECONCILE-COVERAGE.md`.

`docs/RECONCILE-SPEC.md` §6 makes coverage the published number and precision the
honest quality metric.  This script owns the source-agnostic corpus so the
published table and `tests/test_constraint_conformance.py` cannot drift: the
conformance suite imports `CORPUS` and `truthful_claim` from here.

Coverage is expected to be 100% by construction — one record per enumerated
constraint — so the number that carries information is the tier distribution:
how much the equivalence ladder actually decides versus how much it abstains on.
Run with no arguments to rewrite the document, or `--check` to fail when the
committed document no longer matches what the engine produces.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from sidq.claims.models import Claim
from sidq.claims.reconcile import ConstraintReconciler
from sidq.graph.live_source import LiveConstraint

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs" / "RECONCILE-COVERAGE.md"
URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.raw.orders,PROD)"

# One constraint per shape the acquisition layer can enumerate, including the
# two that exist to prove the ladder abstains rather than guesses: an opaque
# future constraint kind, and a partial unique index.
CORPUS: tuple[LiveConstraint, ...] = (
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
        "CREATE UNIQUE INDEX idx_orders_active_slug ON raw.orders (active_slug) "
        "WHERE deleted_at IS NULL",
        predicate="(deleted_at IS NULL)",
        is_partial=True,
    ),
)


class _MemorySource:
    def __init__(self, constraints: tuple[LiveConstraint, ...]) -> None:
        self.constraints = constraints

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        return self.constraints


def claim(
    claim_type: str,
    column: str,
    *,
    values: tuple[str, ...] | None = None,
    expr: str | None = None,
) -> Claim:
    return Claim(
        claim_type,  # type: ignore[arg-type] -- fixtures use Claim's public vocabulary
        column,
        values=values,
        expr=expr,
        source_sentence="truthful in-memory catalog fixture",
        confidence=1.0,
    )


def truthful_claim(constraint: LiveConstraint) -> Claim:
    """The most faithful claim a catalog could make about this constraint."""
    if constraint.kind in {"primary_key", "unique"}:
        return claim("unique", ", ".join(constraint.columns))
    if constraint.kind == "check":
        expression = constraint.definition.removeprefix("CHECK (").removesuffix(")")
        return claim("expression", constraint.columns[0], expr=expression)
    # Claim has no exclude or opaque vocabulary.  Keeping the raw statement as an
    # expression is honest but intentionally asks the ladder to abstain.
    return claim("expression", constraint.columns[0], expr=constraint.definition)


def measure() -> dict[str, object]:
    """Reconcile the corpus against a maximally truthful catalog."""
    claims = tuple(truthful_claim(constraint) for constraint in CORPUS)
    records = ConstraintReconciler(_MemorySource(CORPUS)).reconcile(URN, claims)
    by_name = {record.detail.get("constraint_name"): record for record in records}

    rows = []
    for constraint in CORPUS:
        record = by_name.get(constraint.name)
        assert record is not None, f"no record for {constraint.name}"
        rows.append(
            {
                "name": constraint.name,
                "kind": constraint.kind,
                "represented": record.detail.get("canonical") is not None,
                "verdict": record.kind,
                "tier": str(record.detail.get("tier", "")),
            }
        )
    return {
        "enumerated": len(CORPUS),
        "records": len(records),
        "rows": rows,
        "false_accusations": sum(
            1 for row in rows if row["verdict"] == "constraint_contradicts_catalog"
        ),
    }


def _render(result: dict[str, object]) -> str:
    rows = result["rows"]
    assert isinstance(rows, list)
    enumerated = result["enumerated"]
    records = result["records"]
    decided = sum(1 for row in rows if row["tier"] not in {"T4", ""})
    represented = sum(1 for row in rows if row["represented"])

    by_kind: dict[str, Counter[str]] = {}
    kind_totals: Counter[str] = Counter()
    kind_represented: Counter[str] = Counter()
    for row in rows:
        kind = str(row["kind"])
        kind_totals[kind] += 1
        if row["represented"]:
            kind_represented[kind] += 1
        by_kind.setdefault(kind, Counter())[str(row["tier"])] += 1

    lines = [
        "# Constraint reconciliation — measured coverage",
        "",
        "> Generated by `scripts/measure_reconcile.py`. Do not edit by hand.",
        "> The corpus lives in that script and is imported by",
        "> `tests/test_constraint_conformance.py`, so this document and the",
        "> conformance suite cannot disagree.",
        "",
        "Every constraint is reconciled against the most faithful claim a catalog",
        "could make about it, so any accusation here would be false by construction.",
        "",
        "## Headline",
        "",
        "| Measure | Value |",
        "| --- | ---: |",
        f"| Constraints enumerated | {enumerated} |",
        f"| Evidence records emitted | {records} |",
        f"| Coverage | {records / enumerated:.0%} |",
        (
            f"| Canonically represented | {represented}/{enumerated} "
            f"({represented / enumerated:.0%}) |"
        ),
        (
            f"| Decided by the ladder (T0–T3) | {decided}/{enumerated} "
            f"({decided / enumerated:.0%}) |"
        ),
        (
            f"| Abstentions (T4) | {enumerated - decided}/{enumerated} "
            f"({(enumerated - decided) / enumerated:.0%}) |"
        ),
        f"| **False accusations** | **{result['false_accusations']}** |",
        "",
        "Coverage is 100% by construction: one record per enumerated constraint,",
        "emitted before any attempt to recognise its SQL. The number that carries",
        "information is the abstention share — what the ladder declines to decide.",
        "",
        "## Per constraint kind",
        "",
        "| Kind | Enumerated | Represented | Tier distribution |",
        "| --- | ---: | ---: | --- |",
    ]
    for kind in sorted(kind_totals):
        tiers = ", ".join(
            f"{tier or 'none'}×{count}" for tier, count in sorted(by_kind[kind].items())
        )
        lines.append(
            f"| `{kind}` | {kind_totals[kind]} | "
            f"{kind_represented[kind]}/{kind_totals[kind]} | {tiers} |"
        )

    lines += [
        "",
        "## Per constraint",
        "",
        "| Constraint | Kind | Represented | Verdict | Tier |",
        "| --- | --- | :---: | --- | --- |",
    ]
    for row in rows:
        mark = "yes" if row["represented"] else "no"
        lines.append(
            f"| `{row['name']}` | `{row['kind']}` | {mark} | "
            f"`{row['verdict']}` | {row['tier'] or 'none'} |"
        )

    lines += [
        "",
        "## How to read an abstention",
        "",
        "A `T4` record is not a failure. It states that the engine enumerated the",
        "constraint, retained its verbatim DDL, and refused to claim equivalence it",
        "could not prove. `exclude` and `opaque` kinds abstain because `Claim` has no",
        "vocabulary for them; a partial unique index abstains because a conditional",
        "constraint cannot prove an unconditional catalog claim.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed document is stale",
    )
    arguments = parser.parse_args()

    rendered = _render(measure())
    if arguments.check:
        current = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.exists() else ""
        if current != rendered:
            print(f"{DOCUMENT} is stale; rerun scripts/measure_reconcile.py")
            return 1
        print(f"{DOCUMENT} is current")
        return 0
    DOCUMENT.write_text(rendered, encoding="utf-8")
    print(f"wrote {DOCUMENT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
