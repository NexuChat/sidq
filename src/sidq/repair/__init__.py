"""The repair agent: propose from catalog evidence, prove, then write.

`src/sidq/agent/` finds what the catalog gets wrong. This decides what can be done
about it — and, more usefully, what cannot. Its output is not a list of
suggestions; it is a list of changes the deterministic engine has already re-run
and confirmed, alongside the ones it disproved.
"""

from sidq.repair.apply import (
    ApplyOutcome,
    apply_repairs,
    render_applied,
    render_plan,
)
from sidq.repair.proposals import UNREPAIRABLE, Proposal, propose, propose_all
from sidq.repair.prove import RepairOutcome, RepairPlan, prove, simulate, unfixed

__all__ = [
    "UNREPAIRABLE",
    "ApplyOutcome",
    "Proposal",
    "RepairOutcome",
    "RepairPlan",
    "apply_repairs",
    "propose",
    "propose_all",
    "prove",
    "render_applied",
    "render_plan",
    "simulate",
    "unfixed",
]
