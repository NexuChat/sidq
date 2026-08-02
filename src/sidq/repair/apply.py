"""Write the proven repairs back through the official MCP mutation tools.

The only side effect in the package. Everything that decides *what* to write is
pure and testable without a catalog; this is the narrow boundary where a decision
becomes a change to someone's metadata, and it is deliberately dull.

Three properties it does not have, on purpose:

It does not decide. It writes exactly the arguments `prove` cleared, and refuses
anything the plan did not clear — including a proposal that was individually
proven but part of a set that failed joint verification.

It does not batch. One MCP call per proposal, so a failure names the asset it
failed on instead of leaving a bulk mutation half-applied and unattributed.

It does not stop. A failed write is recorded and the next one is attempted, so a
single permission error on one asset cannot silently abandon the rest.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from sidq.gates.self_contradiction import CatalogEntity, CatalogField, CatalogSnapshot
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.receipt.write import ToolCaller
from sidq.repair.proposals import Proposal
from sidq.repair.prove import RepairPlan, verify_applied


class DirectDatasetReader(Protocol):
    """The post-write seam: entity reads by URN, never catalog search."""

    def get_dataset(self, urn: str) -> DatasetInfo | None: ...


type SnapshotReader = Callable[[Sequence[Proposal], float], CatalogSnapshot]


@dataclass(frozen=True, slots=True)
class PostWriteFinding:
    """A structured gate finding observed in the direct post-write snapshot."""

    kind: str
    subject: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """One attempted mutation. Failures are reported, never raised."""

    proposal: Proposal
    applied: bool
    detail: str = ""
    verified: bool = False
    unresolved: tuple[PostWriteFinding, ...] = ()
    collateral: tuple[PostWriteFinding, ...] = ()

    @property
    def closed(self) -> bool:
        """Only a mutation proven by a live re-read closes its finding."""
        return self.applied and self.verified

    @property
    def status(self) -> str:
        if self.detail == "dry run — nothing written":
            return "dry_run"
        if not self.applied:
            return "write_failed"
        if self.verified:
            return "applied_verified"
        return "applied_unverified"


def apply_repairs(
    plan: RepairPlan, tool_caller: ToolCaller, *, dry_run: bool = True
) -> list[ApplyOutcome]:
    """Write what the engine proved. ``dry_run`` is the default for a reason."""
    outcomes: list[ApplyOutcome] = []
    for proposal in plan.writable:
        if dry_run:
            outcomes.append(ApplyOutcome(proposal, False, "dry run — nothing written"))
            continue
        try:
            tool_caller(proposal.tool, proposal.arguments)
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            outcomes.append(ApplyOutcome(proposal, False, type(error).__name__))
            continue
        outcomes.append(
            ApplyOutcome(proposal, True, "mutation acknowledged; awaiting live read")
        )
    return outcomes


def refresh_snapshot(
    before: CatalogSnapshot,
    proposals: Sequence[Proposal],
    graph: DirectDatasetReader,
) -> CatalogSnapshot:
    """Replace mutated entities from direct MCP entity/aspect reads.

    Repairs only alter tags, terms, and owners. Lineage and every untouched entity
    therefore remain the exact baseline used for the pre-write proof. Each target
    is fetched by URN through ``get_dataset``; this path never searches an
    asynchronously updated index.
    """
    existing = {entity.urn: entity for entity in before.entities}
    refreshed: dict[str, CatalogEntity] = {}
    urns = sorted({urn for proposal in proposals for urn, _ in proposal.targets})
    for urn in urns:
        prior = existing.get(urn)
        if prior is None:
            raise RuntimeError(f"repair target is absent from baseline: {urn}")
        dataset = graph.get_dataset(urn)
        if dataset is None:
            raise RuntimeError(f"repair target is absent from live read: {urn}")
        glossary = dict(dataset.glossary)
        refreshed[urn] = replace(
            prior,
            fields=tuple(_catalog_field(field, glossary) for field in dataset.fields),
            tags=_markers(dataset.tags, dataset.terms, glossary),
            owners=tuple(sorted(dataset.owners)),
            deprecated=dataset.deprecated,
            schema_available=True,
        )
    return replace(
        before,
        entities=tuple(refreshed.get(entity.urn, entity) for entity in before.entities),
    )


def verify_repairs(
    before: CatalogSnapshot,
    outcomes: Sequence[ApplyOutcome],
    read_snapshot: SnapshotReader,
    *,
    timeout: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ApplyOutcome]:
    """Poll direct catalog reads until acknowledged writes are proven or time out."""
    acknowledged = [item for item in outcomes if item.applied]
    if not acknowledged:
        return list(outcomes)
    proposals = [item.proposal for item in acknowledged]
    deadline = monotonic() + max(timeout, 0.0)
    delay = 0.05
    checked = list(outcomes)
    last_detail = "direct live verification did not complete"
    while True:
        try:
            read_timeout = max(deadline - monotonic(), 0.0)
            proof = verify_applied(
                before, read_snapshot(proposals, read_timeout), proposals
            )
        except Exception as error:  # noqa: BLE001 - read transports raise several types
            last_detail = f"direct live read failed: {type(error).__name__}"
        else:
            last_detail = proof.detail
            unresolved = tuple(
                PostWriteFinding(kind, subject) for kind, subject in proof.unresolved
            )
            collateral = tuple(
                PostWriteFinding(kind, subject) for kind, subject in proof.collateral
            )
            resolved = set(proof.resolved) if not proof.collateral else set()
            checked = [
                replace(
                    item,
                    verified=(item.proposal.finding_kind, item.proposal.subject)
                    in resolved,
                    detail=proof.detail,
                    unresolved=unresolved,
                    collateral=collateral,
                )
                if item.applied
                else item
                for item in outcomes
            ]
            if proof.verified:
                return checked
        remaining = deadline - monotonic()
        if remaining <= 0:
            return [
                replace(item, detail=last_detail)
                if item.applied and not item.verified
                else item
                for item in checked
            ]
        sleep(min(delay, remaining))
        delay = min(delay * 2, 1.0)


def render_plan(plan: RepairPlan, *, dry_run: bool = True) -> list[str]:
    """The repair report: what was proven, what was refused, and why."""
    summary = plan.summary()
    lines = [
        "Repairs the catalog's own evidence can prove",
        "",
        f"  proposed        {summary['proposed']}",
        f"  proven          {summary['proven']}",
        f"  rejected        {summary['rejected']}",
    ]
    if not plan.jointly_verified:
        lines.append(f"  BLOCKED         applied together: {plan.joint_reason}")
    lines.append("")

    if plan.proven:
        lines.append("Proven — the engine re-ran and confirmed each one:")
        for outcome in plan.proven:
            lines.extend(_render_proposal(outcome.proposal))
        lines.append("")
    if plan.rejected:
        lines.append("Refused — proposed, then disproved:")
        for outcome in plan.rejected:
            lines.append(f"  {_short(outcome.proposal.subject)}")
            lines.append(f"    {outcome.reason}")
            for item in outcome.collateral[:3]:
                lines.append(f"    would introduce: {item}")
        lines.append("")

    lines.append(
        "Nothing was written (add --apply to write)."
        if dry_run
        else "Repairs approved for an MCP write attempt."
    )
    return lines


def render_applied(outcomes: Sequence[ApplyOutcome]) -> list[str]:
    written = [item for item in outcomes if item.applied]
    verified = [item for item in outcomes if item.closed]
    unverified = [item for item in outcomes if item.applied and not item.verified]
    failed = [
        item
        for item in outcomes
        if not item.applied and item.detail != "dry run — nothing written"
    ]
    lines = [
        f"  repairs written   {len(written)} of {len(outcomes)}",
        f"  repairs verified  {len(verified)} of {len(written)}",
    ]
    for item in failed[:5]:
        lines.append(f"    {_short(item.proposal.subject)}: {item.detail}")
    for item in unverified[:5]:
        lines.append(
            f"    {_short(item.proposal.subject)}: applied_unverified — {item.detail}"
        )
        for collateral in item.collateral[:3]:
            suffix = f" — {collateral.detail}" if collateral.detail else ""
            lines.append(
                f"      collateral: {collateral.kind} on {collateral.subject}{suffix}"
            )
    return lines


def _catalog_field(field: SchemaField, glossary: dict[str, str]) -> CatalogField:
    return CatalogField(
        path=field.path,
        description=field.description,
        tags=_markers(
            field.tags,
            field.terms,
            glossary,
        ),
    )


def _markers(
    tags: Sequence[str], terms: Sequence[str], glossary: dict[str, str]
) -> tuple[str, ...]:
    return tuple(sorted(set(tags) | {glossary.get(term, term) for term in terms}))


def _render_proposal(proposal: Proposal) -> list[str]:
    lines = [
        f"  {_short(proposal.subject)}",
        f"    {proposal.tool}({_arguments(proposal)})",
        f"    because {proposal.rationale}",
    ]
    targets = proposal.targets
    if len(targets) > 1:
        lines.append(f"    covers {len(targets)} columns in one call:")
        lines.extend(
            f"      {_short(f'{urn}#{column}' if column else urn)}"
            for urn, column in targets[:6]
        )
        if len(targets) > 6:
            lines.append(f"      ... and {len(targets) - 6} more")
    return lines


def _arguments(proposal: Proposal) -> str:
    values = (
        proposal.arguments.get("tag_urns")
        or proposal.arguments.get("term_urns")
        or proposal.arguments.get("owner_urns")
    )
    return ", ".join(str(value) for value in values or ())


def _short(subject: str) -> str:
    urn, _, column = subject.partition("#")
    name = urn.rsplit(",", 2)[-2] if "," in urn else urn
    return f"{name}#{column}" if column else name
