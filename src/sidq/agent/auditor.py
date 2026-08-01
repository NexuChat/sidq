"""An autonomous catalog-truth auditor.

Point it at a DataHub it has never seen. It decides what to examine, in what
order, how deep to go, and when to stop — then reports what it covered and,
just as importantly, what it did not.

**Why this is an agent and not the audit script beside it.**
`examples/03-catalog-truth-report/generate_report.py` reads everything, runs every
check, and dumps a report: a fixed sequence that behaves identically regardless of
what it finds. This chooses its next target from what it has already observed. It
ranks assets by how much damage a lie about them would do, spends a bounded budget
on the worst first, and — the part that makes it a loop rather than a sort — lets
what it finds change what it looks at next: an asset that turns out to be lied
about promotes its neighbours ahead of their static score, because contradictions
cluster. Given the same catalog it is deterministic; given a *different* one it
does something different.

**What it never does.** It does not decide truth with a model, and it does not
convert an unperformed check into a clean bill of health. Every judgment here is
the deterministic engine's; the agent chooses *where to point it*, which is the
part that was missing.

**It can also remember — through the catalog, not beside it.** Given the prior
receipts a previous run wrote back (`sidq.agent.memory`), it skips assets whose
receipt still holds and spends the whole budget on assets no run has reached.
Coverage converges across runs under a fixed budget, and because the memory is
the catalog itself, any sidq instance resumes where any other stopped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sidq.agent.memory import PriorReceipt
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogSnapshot,
    SelfContradictionGate,
)
from sidq.models import Evidence

# An audit that silently stops halfway is worse than one that admits it stopped,
# so the budget is explicit and what it excluded is always reported.
DEFAULT_BUDGET = 250

# Findings that say something about how the metadata was *written*, and therefore
# predict the same mistake next door: one job, one migration, one bad assumption
# applied across a set of models. These promote a neighbour.
#
# Ownership gaps and deprecation are deliberately not here. They are per-asset
# governance facts, not contagious ones — an unowned table tells you nothing about
# whether its neighbour has an owner. Promoting on them was tried and it spent the
# budget chasing a signal that does not cluster: on the live catalog it displaced
# every `lineage_field_missing` finding at the same budget.
CONTAGIOUS_FINDINGS = frozenset(
    {
        "doc_references_missing_column",
        "lineage_field_missing",
        "orphan_lineage",
        "pii_leak_untagged",
    }
)


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
    # Examined, but nothing could be established either way — never 'clean'.
    unestablished: list[str] = field(default_factory=list)
    # (urn, reason) — skipped because a prior receipt still holds. Not examined
    # this run and never counted as if it had been; the receipt vouches, we don't.
    vouched: list[tuple[str, str]] = field(default_factory=list)
    order: list[Target] = field(default_factory=list)
    # Evidence kept per asset so a receipt can be built later without re-auditing.
    evidence_by_urn: dict[str, list[Evidence]] = field(default_factory=dict)
    # (because_of, promoted) — the agent reacting to what it found, made visible.
    promoted: list[tuple[str, str]] = field(default_factory=list)

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
            "unestablished": len(self.unestablished),
            "vouched_by_receipt": len(self.vouched),
            "promoted_by_a_finding": len(self.promoted),
        }


class CatalogAuditor:
    """Perceive a catalog, decide what matters, examine it, report honestly."""

    def __init__(
        self,
        snapshot: CatalogSnapshot,
        *,
        budget: int = DEFAULT_BUDGET,
        gate: SelfContradictionGate | None = None,
        prior: Mapping[str, PriorReceipt] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._budget = max(0, budget)
        self._gate = gate or SelfContradictionGate()
        # What the catalog remembers from previous runs (`sidq.agent.memory`).
        # Empty means amnesia, and amnesia is the safe default: nothing is ever
        # skipped on the strength of a receipt nobody read.
        self._prior = dict(prior or {})

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
        """Examine the catalog, letting what is found change what is looked at next.

        The order is not fixed up front. An asset that turns out to be lied about
        promotes its immediate neighbours, because catalog contradictions cluster:
        a model with a bad lineage edge usually has siblings written by the same
        job, in the same migration, with the same mistake. A human auditor who
        found one would look next door rather than continue down an alphabetical
        list, and this does the same.

        An earlier version of this method sorted once and iterated, while the
        docstring claimed it deepened where a finding warranted it. It did not —
        every target got identical treatment and nothing branched on a result. The
        claim was rhetoric; this is the loop that makes it true.

        Still deterministic: promotion depends only on catalog contents, so the
        same catalog always yields the same transcript.
        """
        plan = self.plan()
        result = AuditRun(order=plan)
        by_urn = {target.urn: target for target in plan}
        queue = list(plan)
        promoted: set[str] = set()
        seen: set[str] = set()

        while queue:
            target = queue.pop(0)
            if target.urn in seen:
                continue
            seen.add(target.urn)

            # A holding receipt is a memoised verdict: the engine is
            # deterministic, so unchanged asset + unchanged policy would
            # reproduce exactly what a previous run already wrote back.
            # Re-deriving it would spend budget to learn nothing, and the
            # budget's whole purpose is to reach the assets no run has seen.
            # `holds()` was recomputed by this reader at recall time — stale,
            # blocked, and absent receipts all fail it and stay in the queue.
            # Not even a promotion pierces this: a neighbour's lie is evidence
            # about the neighbour, not a change to this asset's content.
            prior = self._prior.get(target.urn)
            if prior is not None and prior.holds:
                result.vouched.append((target.urn, prior.reason))
                continue

            if len(result.examined) >= self._budget:
                # Not silence: everything the budget did not reach is named, so
                # the report can never read as complete coverage.
                result.deferred.append(target)
                continue

            result.examined.append(target.urn)
            collected = self._examine(target)
            result.evidence_by_urn[target.urn] = collected
            found = [
                item for item in collected if not item.kind.endswith("_unverifiable")
            ]
            result.unverifiable.extend(
                item for item in collected if item.kind.endswith("_unverifiable")
            )
            result.findings.extend(found)
            if not found:
                # "Nothing found" and "nothing checkable" are different answers.
                # An asset whose only evidence is unverifiable was looked at and
                # not established, so calling it clean would be the exact mistake
                # this project exists to catch — in its own report, no less.
                if any(item.kind.endswith("_unverifiable") for item in collected):
                    result.unestablished.append(target.urn)
                else:
                    result.verified.append(target.urn)
                continue

            # Only a contagious finding redirects the budget. A lie about how the
            # metadata was written predicts the same lie next door; a missing owner
            # does not.
            if not any(item.kind in CONTAGIOUS_FINDINGS for item in found):
                continue
            for urn in self._neighbours(target.urn):
                if urn in seen or urn in promoted or urn not in by_urn:
                    continue
                promoted.add(urn)
                result.promoted.append((target.urn, urn))
                queue.insert(0, by_urn[urn])
        return result

    def _neighbours(self, urn: str) -> list[str]:
        """Assets one lineage hop away, in a stable order."""
        return sorted(
            {edge.target_urn for edge in self._snapshot.edges if edge.source_urn == urn}
            | {
                edge.source_urn
                for edge in self._snapshot.edges
                if edge.target_urn == urn
            }
        )

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
            # Carried, not defaulted. Dropping it would let slicing quietly turn a
            # bounded read into a complete one, and every asset whose lineage was
            # never fetched would come back looking clean.
            self._snapshot.field_lineage_resolved,
        )

        # Keep only what this target is answerable for. Subject-prefix matching
        # covers the asset itself and its columns (`urn#field`), and that is the
        # right rule for every finding whose subject is a real asset — a source
        # must not inherit its neighbour's broken schema.
        #
        # One kind escapes it. An orphan edge's subject is the URN that is
        # *missing* from the catalog, so it can never prefix-match any target,
        # and the filter silently discarded every orphan the gate produced: the
        # check ran, found a real contradiction, and the agent threw it away. An
        # independent review of the detection logic caught it. Orphans are
        # therefore attributed to the real endpoint that claims the dangling
        # edge, which is the asset a reader can actually act on.
        def _concerns_target(item: Evidence) -> bool:
            if item.subject.startswith(target.urn):
                return True
            if item.kind != "orphan_lineage":
                return False
            edge = item.detail.get("edge") or {}
            return target.urn in (
                edge.get("source_dataset"),
                edge.get("target_dataset"),
            )

        return [
            item
            for item in self._gate.collect((), _Scoped(slice_))
            if _concerns_target(item)
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
    if result.vouched:
        # Distinct from 'verified clean' on purpose: these were not examined
        # this run. The receipt vouches for them, and the line says whose word
        # the reader is taking.
        lines.append(
            f"  vouched         {len(result.vouched)} "
            "(a prior receipt still holds; budget spent elsewhere)"
        )
    if result.unestablished:
        lines.append(
            f"  NOT established {len(result.unestablished)} "
            "(examined, but nothing could be checked — not clean)"
        )
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
    snapshot: CatalogSnapshot,
    *,
    budget: int = DEFAULT_BUDGET,
    prior: Mapping[str, PriorReceipt] | None = None,
) -> tuple[AuditRun, Sequence[str]]:
    result = CatalogAuditor(snapshot, budget=budget, prior=prior).run()
    return result, render(result, catalog=f"{len(snapshot.entities)} entities")
