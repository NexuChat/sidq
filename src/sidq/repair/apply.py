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

from collections.abc import Sequence
from dataclasses import dataclass

from sidq.receipt.write import ToolCaller
from sidq.repair.proposals import Proposal
from sidq.repair.prove import RepairPlan


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """One attempted mutation. Failures are reported, never raised."""

    proposal: Proposal
    applied: bool
    detail: str = ""


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
        outcomes.append(ApplyOutcome(proposal, True))
    return outcomes


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
        else "Written through the official DataHub MCP mutation tools."
    )
    return lines


def render_applied(outcomes: Sequence[ApplyOutcome]) -> list[str]:
    written = [item for item in outcomes if item.applied]
    failed = [
        item
        for item in outcomes
        if not item.applied and item.detail != "dry run — nothing written"
    ]
    lines = [f"  repairs written   {len(written)} of {len(outcomes)}"]
    for item in failed[:5]:
        lines.append(f"    {_short(item.proposal.subject)}: {item.detail}")
    return lines


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
