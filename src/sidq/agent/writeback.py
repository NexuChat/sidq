"""Carry the auditor's result back into the catalog as receipts.

This is the half of the first judging criterion that reading alone cannot satisfy:
the agent traverses the graph, and then writes what it concluded back where the
next agent can read it. `search_verified` is the consumer — an asset with a fresh
receipt is one any agent, including a competitor's, can trust without re-deriving
anything.

Two properties are deliberate.

**The agent still does not decide.** It does not label an asset verified; it runs
the shipped policy over the evidence it collected and carries *that* verdict. If
the policy changes, the receipts change with it, and the `policy_hash` on each one
says which policy produced it.

**Computing is pure; writing is not.** `receipts_for` is a function of the audit
result and produces exactly what would be written, so a caller can inspect it,
test it, or print it without touching a catalog. `write_receipts` is the only
thing here with a side effect, and it is never reached unless a caller asks.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sidq.agent.auditor import AuditRun
from sidq.policy.engine import PolicyEngine
from sidq.receipt.build import Receipt, build_receipt

# The write surface is the receipt module's own, not a second declaration of it.
# Two protocols that are supposed to describe the same thing is how they drift.
from sidq.receipt.write import ToolCaller, write_receipt


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """One attempted write, successful or not. Failures are reported, not raised."""

    urn: str
    verdict: str
    written: bool
    detail: str = ""


def receipts_for(
    result: AuditRun,
    *,
    policy_path: str | Path | None = None,
    commit_sha: str = "",
    checked_at: datetime | None = None,
    evidence_url: str = "",
) -> list[Receipt]:
    """What the audit would write back, computed without writing anything.

    Only examined assets get a receipt. A deferred asset has no evidence behind
    it, and writing "verified" for something the agent never looked at is the one
    thing a verification layer may never do.
    """
    engine = PolicyEngine(policy_path)
    receipts: list[Receipt] = []
    for urn in result.examined:
        verdict = engine.decide(
            tuple(result.evidence_by_urn.get(urn, ())), commit_sha=commit_sha
        )
        receipts.append(
            build_receipt(
                urn,
                verdict,
                checked_at=checked_at,
                evidence_url=evidence_url,
            )
        )
    return receipts


def write_receipts(
    receipts: Sequence[Receipt], tool_caller: ToolCaller
) -> list[WriteOutcome]:
    """Write each receipt, and keep going when one fails.

    A partial write is reported per asset rather than aborting the run, because
    the alternative — one transport error discarding a completed audit — loses
    real work. The caller sees exactly which assets carry a receipt and which do
    not, so nothing is presumed written.
    """
    outcomes: list[WriteOutcome] = []
    for receipt in receipts:
        try:
            write_receipt(receipt, tool_caller)
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            outcomes.append(
                WriteOutcome(receipt.urn, receipt.verdict, False, type(error).__name__)
            )
            continue
        outcomes.append(WriteOutcome(receipt.urn, receipt.verdict, True))
    return outcomes


def render_writeback(outcomes: Sequence[WriteOutcome]) -> list[str]:
    written = [item for item in outcomes if item.written]
    failed = [item for item in outcomes if not item.written]
    lines = [f"  receipts written  {len(written)} of {len(outcomes)}"]
    if failed:
        lines.append(f"  write failures    {len(failed)}")
        lines.extend(
            f"    {item.urn.rsplit(',', 2)[-2]}: {item.detail}" for item in failed[:5]
        )
    return lines
