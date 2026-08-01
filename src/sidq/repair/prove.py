"""Prove a repair before anyone writes it.

This is the part that makes the repair agent worth having. Proposing a fix is
cheap and every LLM can do it; the question a data team actually has is whether
the fix is correct and whether it breaks something else. Both are answerable here
without touching the catalog, because the same deterministic engine that found the
problem can be re-run against the catalog as it *would* be.

So every proposal is put through three questions, in order:

1. Does it resolve the finding it was made for? A proposal that leaves the
   contradiction standing is worthless no matter how sensible it reads.
2. Does it introduce a finding that was not there before? A repair that trades one
   contradiction for another is a regression with good intentions.
3. Do the survivors still hold *together*? Proposals proven one at a time can
   still conflict once applied as a set, so the whole set is re-proven jointly.

Only what survives all three is offered for writing. Everything else is reported
as rejected, with the engine's reason — the agent publishes its own failures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogSnapshot,
    SelfContradictionGate,
)
from sidq.models import Evidence
from sidq.repair.proposals import Proposal

_FindingKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class RepairOutcome:
    """One proposal and what the engine said about it. Never a self-assessment."""

    proposal: Proposal
    proven: bool
    resolved: bool
    collateral: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """Everything proposed, split by what the engine could prove."""

    proven: tuple[RepairOutcome, ...] = ()
    rejected: tuple[RepairOutcome, ...] = ()
    jointly_verified: bool = False
    joint_reason: str = ""

    @property
    def writable(self) -> tuple[Proposal, ...]:
        """Nothing is writable unless the set was also proven as a set."""
        if not self.jointly_verified:
            return ()
        return tuple(outcome.proposal for outcome in self.proven)

    def summary(self) -> dict[str, object]:
        by_kind: dict[str, int] = {}
        for outcome in self.proven:
            kind = outcome.proposal.finding_kind
            by_kind[kind] = by_kind.get(kind, 0) + 1
        return {
            "proposed": len(self.proven) + len(self.rejected),
            "proven": len(self.proven),
            "rejected": len(self.rejected),
            "proven_by_finding_kind": dict(sorted(by_kind.items())),
            "jointly_verified": self.jointly_verified,
        }


def prove(
    snapshot: CatalogSnapshot,
    proposals: Sequence[Proposal],
    *,
    gate: SelfContradictionGate | None = None,
) -> RepairPlan:
    """Re-run the deterministic engine against the catalog each repair would create."""
    gate = gate or SelfContradictionGate()
    baseline = _findings(gate, snapshot)
    proven: list[RepairOutcome] = []
    rejected: list[RepairOutcome] = []

    for proposal in proposals:
        target = (proposal.finding_kind, proposal.subject)
        after = _findings(gate, simulate(snapshot, proposal))
        resolved = target not in after
        collateral = tuple(
            sorted(f"{kind} on {subject}" for kind, subject in after - baseline)
        )
        if resolved and not collateral:
            proven.append(
                RepairOutcome(
                    proposal, True, True, (), "engine confirms the finding is gone"
                )
            )
        elif not resolved:
            rejected.append(
                RepairOutcome(
                    proposal,
                    False,
                    False,
                    collateral,
                    "the finding survives this change, so it is not a repair",
                )
            )
        else:
            rejected.append(
                RepairOutcome(
                    proposal,
                    False,
                    True,
                    collateral,
                    f"resolves the finding but introduces {len(collateral)} new one(s)",
                )
            )

    jointly, reason = _prove_together(
        gate, snapshot, baseline, [outcome.proposal for outcome in proven]
    )
    return RepairPlan(tuple(proven), tuple(rejected), jointly, reason)


def _prove_together(
    gate: SelfContradictionGate,
    snapshot: CatalogSnapshot,
    baseline: set[_FindingKey],
    proposals: Sequence[Proposal],
) -> tuple[bool, str]:
    """Individually safe is not the same as safe together, so check the set too."""
    if not proposals:
        return True, "nothing to apply"
    combined = snapshot
    for proposal in proposals:
        combined = simulate(combined, proposal)
    after = _findings(gate, combined)
    introduced = sorted(f"{kind} on {subject}" for kind, subject in after - baseline)
    if introduced:
        return False, f"applied together they introduce: {', '.join(introduced[:3])}"
    unresolved = [
        proposal
        for proposal in proposals
        if (proposal.finding_kind, proposal.subject) in after
    ]
    if unresolved:
        return False, f"{len(unresolved)} finding(s) survive when the set is applied"
    return True, "the whole set resolves cleanly and introduces nothing"


def simulate(snapshot: CatalogSnapshot, proposal: Proposal) -> CatalogSnapshot:
    """The catalog as it would be. Pure — this is why proving costs no writes."""
    columns: dict[str, set[str | None]] = {}
    for urn, column in proposal.targets:
        columns.setdefault(urn, set()).add(column)
    entities = tuple(
        _apply_to_entity(entity, proposal, columns[entity.urn])
        if entity.urn in columns
        else entity
        for entity in snapshot.entities
    )
    return CatalogSnapshot(
        entities,
        snapshot.edges,
        # The simulated catalog is exactly as complete as the one it was built
        # from: a repair changes tags, never which assets exist.
        entities_complete=snapshot.entities_complete,
        field_lineage_resolved=snapshot.field_lineage_resolved,
    )


def _apply_to_entity(
    entity: CatalogEntity, proposal: Proposal, columns: set[str | None]
) -> CatalogEntity:
    if proposal.tool == "add_owners":
        owners = _added(entity.owners, proposal.arguments.get("owner_urns"))
        return replace(entity, owners=owners)
    if proposal.tool in ("add_tags", "add_terms"):
        tags = proposal.arguments.get("tag_urns") or proposal.arguments.get("term_urns")
        if None in columns:
            entity = replace(entity, tags=_added(entity.tags, tags))
        named = {column for column in columns if column is not None}
        if not named:
            return entity
        return replace(
            entity,
            fields=tuple(
                replace(item, tags=_added(item.tags, tags))
                if item.path in named
                else item
                for item in entity.fields
            ),
        )
    return entity


def _added(existing: tuple[str, ...], values: Any) -> tuple[str, ...]:
    return tuple(sorted(set(existing) | {str(value) for value in values or ()}))


def _findings(
    gate: SelfContradictionGate, snapshot: CatalogSnapshot
) -> set[_FindingKey]:
    return {
        (item.kind, item.subject)
        for item in gate.collect((), _Supplied(snapshot))
        if not item.kind.endswith("_unverifiable")
    }


class _Supplied:
    """The duck-typed graph the self-contradiction gate reads a snapshot from."""

    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._snapshot


def unfixed(findings: Sequence[Evidence], plan: RepairPlan) -> list[Evidence]:
    """Findings still standing after the plan — the honest remainder."""
    repaired = {
        (outcome.proposal.finding_kind, outcome.proposal.subject)
        for outcome in plan.proven
    }
    # Computed from `proven`, not from what was written: whether a write
    # succeeded is a separate question, and the writer answers it.
    return [item for item in findings if (item.kind, item.subject) not in repaired]
