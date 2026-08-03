"""The repair agent: propose from catalog evidence, prove, then write.

`src/sidq/agent/` finds what the catalog gets wrong. This decides what can be done
about it — and, more usefully, what cannot. Its output is not a list of
suggestions; it is a list of changes the deterministic engine has already re-run
and confirmed, alongside the ones it disproved.
"""

from sidq.repair.apply import (
    ApplyOutcome,
    apply_repairs,
    refresh_snapshot,
    render_applied,
    render_plan,
    verify_repairs,
)
from sidq.repair.proposals import (
    REPAIR_TOOLS,
    UNREPAIRABLE,
    Proposal,
    propose,
    propose_all,
)
from sidq.repair.prove import (
    AppliedRepairProof,
    RepairOutcome,
    RepairPlan,
    prove,
    simulate,
    unfixed,
    verify_applied,
)

__all__ = [
    "REPAIR_TOOLS",
    "UNREPAIRABLE",
    "AppliedRepairProof",
    "ApplyOutcome",
    "Proposal",
    "RepairOutcome",
    "RepairPlan",
    "apply_repairs",
    "propose",
    "propose_all",
    "prove",
    "refresh_snapshot",
    "render_applied",
    "render_plan",
    "simulate",
    "unfixed",
    "verify_applied",
    "verify_repairs",
]
