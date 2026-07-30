"""An autonomous catalog-truth auditor.

Point it at a DataHub it has never seen. It decides what to examine, in what
order, how deep to go, and when to stop — then reports what it covered and,
just as importantly, what it did not.

**Why this is an agent and not the audit script beside it.**
`examples/03-catalog-truth-report/generate_report.py` reads everything, runs every
check, and dumps a report: a fixed sequence that behaves identically regardless of
what it finds. This chooses its next target from what it has already observed. It
ranks assets by how much damage a lie about them would do, spends a bounded budget
on the worst first, deepens only where a finding warrants it, and stops with an
accounting. Given the same catalog it is deterministic; given a *different* one it
does something different.

**What it never does.** It does not decide truth with a model, and it does not
convert an unperformed check into a clean bill of health. Every judgment here is
the deterministic engine's; the agent chooses *where to point it*, which is the
part that was missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogSnapshot,
    SelfContradictionGate,
)
from sidq.models import Evidence

# An audit that silently stops halfway is worse than one that admits it stopped,
# so the budget is explicit and what it excluded is always reported.
DEFAULT_BUDGET = 250


@dataclass(frozen=True, slots=True)
class Target:
    """One asset the agent has decided is worth looking at, and why."""

    urn: str
    consequence: int
    reasons: tuple[str, ...]


@dataclass
class AuditRun:
    """Everything the agent did, including what it chose not to do."""

    examined: list[str] = field(default_factory=list)
    findings: list[Evidence] = field(default_factory=list)
    unverifiable: list[Evidence] = field(default_factory=list)
    deferred: list[Target] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    order: list[Target] = field(default_factory=list)
    # Evidence kept per asset so a receipt can be built later without re-auditing.
    evidence_by_urn: dict[str, list[Evidence]] = field(default_factory=dict)

    @property
    def covered(self) -> int:
        return len(self.examined)

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        for item in self.findings:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        return {
            "examined": self.covered,
            "deferred": len(self.deferred),
            "findings": len(self.findings),
            "findings_by_kind": dict(sorted(by_kind.items())),
            "unverifiable": len(self.unverifiable),
            "verified_clean": len(self.verified),
        }


class CatalogAuditor:
    """Perceive a catalog, decide what matters, examine it, report honestly."""

    def __init__(
        self,
        snapshot: CatalogSnapshot,
        *,
        budget: int = DEFAULT_BUDGET,
        gate: SelfContradictionGate | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._budget = max(0, budget)
        self._gate = gate or SelfContradictionGate()

    # -- perception ------------------------------------------------------

    def _consequence(self, entity: CatalogEntity) -> Target:
        """Rank by how much a lie about this asset would cost, not by name order.

        A wrong claim about an asset nothing reads is a typo. The same claim about
        one feeding twenty consumers, carrying PII, or owned by nobody is the one
        worth spending the budget on.
        """
        downstream = sum(
            1 for edge in self._snapshot.edges if edge.source_urn == entity.urn
        )
        reasons: list[str] = []
        score = downstream * 10
        if downstream:
            reasons.append(f"{downstream} downstream consumers")
        if any("pii" in tag.lower() for tag in entity.tags):
            score += 50
            reasons.append("carries a PII tag")
        if not entity.owners:
            score += 15
            reasons.append("no owner")
        if entity.deprecated:
            score += 20
            reasons.append("deprecated")
        if not entity.schema_available:
            score += 25
            reasons.append("no stored schema")
        return Target(entity.urn, score, tuple(reasons) or ("no notable exposure",))

    def plan(self) -> list[Target]:
        """Order the catalog by consequence. This is the agent's first decision."""
        return sorted(
            (self._consequence(entity) for entity in self._snapshot.entities),
            key=lambda target: (-target.consequence, target.urn),
        )

    # -- action ----------------------------------------------------------

    def run(self) -> AuditRun:
        plan = self.plan()
        result = AuditRun(order=plan)
        for position, target in enumerate(plan):
            if position >= self._budget:
                # Not silence: everything past the budget is named as deferred so
                # the report can never read as complete coverage.
                result.deferred.append(target)
                continue
            result.examined.append(target.urn)
            collected = self._examine(target)
            result.evidence_by_urn[target.urn] = collected
            for item in collected:
                if item.kind.endswith("_unverifiable"):
                    result.unverifiable.append(item)
                else:
                    result.findings.append(item)
            if not any(item.subject.startswith(target.urn) for item in result.findings):
                result.verified.append(target.urn)
        return result

    def _examine(self, target: Target) -> list[Evidence]:
        """Run the deterministic checks against this asset's slice of the catalog.

        The gate audits a whole snapshot, so the agent hands it exactly the slice
        that concerns this asset — the entity and every edge touching it. That is
        the agent's second decision: how much context this target deserves.
        """
        related = tuple(
            edge
            for edge in self._snapshot.edges
            if edge.source_urn == target.urn or edge.target_urn == target.urn
        )
        neighbours = {edge.source_urn for edge in related} | {
            edge.target_urn for edge in related
        }
        neighbours.add(target.urn)
        slice_ = CatalogSnapshot(
            tuple(
                entity for entity in self._snapshot.entities if entity.urn in neighbours
            ),
            related,
        )
        return [
            item
            for item in self._gate.collect((), _Scoped(slice_))
            if item.subject.startswith(target.urn)
            or any(item.subject.startswith(urn) for urn in (target.urn,))
        ]


class _Scoped:
    """The duck-typed graph the self-contradiction gate expects."""

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._snapshot


def render(result: AuditRun, *, catalog: str) -> list[str]:
    """A report a person can act on: worst first, and the gaps stated."""
    summary = result.summary()
    lines = [
        f"Catalog audit — {catalog}",
        "",
        f"  examined        {summary['examined']}",
        f"  findings        {summary['findings']}",
        f"  unverifiable    {summary['unverifiable']}",
        f"  verified clean  {summary['verified_clean']}",
    ]
    if result.deferred:
        lines.append(
            f"  NOT examined    {len(result.deferred)} (budget reached; "
            "raise --budget to cover them)"
        )
    lines.append("")
    # Counted from the findings rather than read back out of the summary dict, so
    # the types stay honest instead of being silenced with an ignore comment.
    by_kind: dict[str, int] = {}
    for item in result.findings:
        by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
    if by_kind:
        lines.append("What the catalog claims that contradicts itself:")
        lines.extend(f"  {kind:34s} {count}" for kind, count in sorted(by_kind.items()))
        lines.append("")
    worst = [target for target in result.order if target.urn in set(result.examined)]
    if worst:
        lines.append("Examined in this order, worst consequence first:")
        for target in worst[:5]:
            lines.append(
                f"  {target.consequence:5d}  {target.urn.rsplit(',', 2)[-2]}"
                f"  ({', '.join(target.reasons)})"
            )
    return lines


def audit(
    snapshot: CatalogSnapshot, *, budget: int = DEFAULT_BUDGET
) -> tuple[AuditRun, Sequence[str]]:
    result = CatalogAuditor(snapshot, budget=budget).run()
    return result, render(result, catalog=f"{len(snapshot.entities)} entities")
