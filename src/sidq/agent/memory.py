"""What the catalog already remembers about its own verification.

The auditor's budget is explicit, and until now it was also amnesiac: every run
started from zero, so a catalog larger than one budget could never be fully
covered — the same worst assets were re-examined forever and the tail was
deferred forever. The state needed to fix that already exists, and it does not
live in a file beside the agent. It lives in the catalog, as the receipts a
previous run wrote back.

`recall` reads those receipts and answers one question per asset: does the
receipt still hold? The judgment is `holds()` — the same one every other
receipt consumer uses — recomputed by this reader, never trusted from storage.
An asset whose receipt holds can be skipped without loss, and the reason is
worth stating precisely: the engine is deterministic, so a receipt is a
memoised verdict keyed by the asset's content and the policy hash. Staleness
is the cache invalidation — asset changed, receipt aged out, policy changed —
and anything stale, blocked, or absent goes back in the queue.

Two properties follow that matter more than the budget arithmetic.

**Coverage converges.** Run after run, the vouched set grows and the budget
flows to assets never yet examined, until the whole catalog is covered — under
a budget that never changed.

**The memory is shared without coordination.** Any sidq instance pointed at
the same catalog reads the same receipts, so one agent resumes where another
stopped. There is no ledger to sync and no server to stand up; the catalog is
the ledger.

Failure is closed in the right direction: if the receipts cannot be read, the
prior is empty and everything is re-examined. Forgetting costs budget;
trusting a receipt that could not be read would cost the thesis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sidq.receipt.read import ToolCaller, get_verification_statuses, holds


@dataclass(frozen=True, slots=True)
class PriorReceipt:
    """One asset's remembered verdict, and this reader's judgment of it."""

    urn: str
    holds: bool
    reason: str


def recall(
    urns: Sequence[str],
    tool_caller: ToolCaller,
    *,
    current_policy_hash: str | None = None,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> dict[str, PriorReceipt]:
    """Read the catalog's receipts for these assets and judge each one now.

    The judgment is per-reader and per-moment on purpose: a receipt that held
    yesterday may be stale today, and a receipt written under an older policy
    stops vouching the moment the policy hash moves. Nothing here mutates the
    catalog, and nothing here decides truth — it only decides whether a prior
    decision still applies.
    """
    statuses = get_verification_statuses(
        urns,
        tool_caller,
        current_policy_hash=current_policy_hash,
        max_age=max_age,
        now=now,
    )
    prior: dict[str, PriorReceipt] = {}
    for urn, status in statuses.items():
        still_holds, reason = holds(status)
        prior[urn] = PriorReceipt(urn=urn, holds=still_holds, reason=reason)
    return prior
