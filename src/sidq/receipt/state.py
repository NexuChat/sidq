"""The separate questions a receipt answers, kept separate.

A receipt read back from the catalog is asked three different things, and the
bug this module exists to remove was answering all of them with one boolean:

1. **Is the receipt applicable?** Is it there, is it readable, does it still
   describe the asset as it is now? That is `ReceiptState`, and it is decided
   without ever looking at the verdict — a refusal that has gone stale is stale
   for exactly the same reasons a pass would be.
2. **What may the agent do?** That is `Action`, and it is authorization. Only a
   current PASS authorizes an agent to continue on its own.
3. **Has the asset been examined?** That is coverage, and it is the question a
   bounded auditor asks before spending budget. Every current receipt covers its
   asset — including a BLOCK, because "we checked and refused" is knowledge, not
   a gap.

Collapsing 2 and 3 is what starved the auditor: a current BLOCK failed the
authorization test, so the resume path treated it as uncovered and re-examined
the same refused asset every run while the untouched tail was never reached. The
fix is not to authorize BLOCK — that would let an agent act on a refusal. It is
to stop asking one function both questions.

Collapsing 1 and 2 is what produced the dishonest readback: a current BLOCK
printed as `NOT VERIFIED`, which is the phrase this project reserves for "nobody
checked". A refusal is the most thoroughly verified answer there is. `NOT
VERIFIED` now belongs to absent, stale, and unreadable receipts alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# The engine emits exactly these. Anything else in the catalog was not written
# by this engine, so it vouches for nothing — a catalog cannot invent a fourth
# verdict and have it read as approval.
VERDICTS = frozenset({"PASS", "WARN", "BLOCK"})


class ReceiptState(StrEnum):
    """Whether the receipt applies, decided independently of what it says."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    ABSENT = "ABSENT"
    # Present but not written by this engine, or not readable as a receipt.
    INVALID = "INVALID"


class Action(StrEnum):
    """What the receipt authorizes the agent reading it to do."""

    CONTINUE = "CONTINUE"
    REVIEW = "REVIEW_OR_ESCALATE"
    STOP = "STOP"
    # Nothing is authorized because nothing applicable is known. The only move
    # is to check again.
    RECHECK = "RECHECK"


@dataclass(frozen=True, slots=True)
class ReceiptJudgment:
    """One reader's answer, at one moment, on all three axes at once.

    Recomputed at read time and never persisted: a receipt that applied an hour
    ago may not apply now, and the catalog is not asked to remember a judgment
    it has no way to keep honest.
    """

    state: ReceiptState
    verdict: str | None
    action: Action
    covers: bool
    reason: str

    @property
    def may_continue(self) -> bool:
        """True only for a current PASS. WARN needs a human; BLOCK is a refusal."""
        return self.action is Action.CONTINUE

    @property
    def refused(self) -> bool:
        """A current receipt recording a refusal — covered, but never a vouch."""
        return self.action is Action.STOP

    @property
    def label(self) -> str:
        """The headline a person reads, which must never overstate or understate.

        A current receipt names all three axes, so `CURRENT RECEIPT · BLOCK ·
        STOP` cannot be mistaken for an unexamined asset. Everything else is
        `NOT VERIFIED`, which now means only what it says.
        """
        if self.state is ReceiptState.CURRENT:
            return f"CURRENT RECEIPT · {self.verdict} · {self.action.value}"
        return "NOT VERIFIED"

    def as_dict(self) -> dict[str, Any]:
        """The judgment as machine-readable fields, for `--json` surfaces."""
        return {
            "receipt_state": str(self.state),
            "action": str(self.action),
            "covers_asset": self.covers,
            "judgment": self.reason,
        }


def judge(status: Mapping[str, Any]) -> ReceiptJudgment:
    """Judge a receipt read back from DataHub, on all three axes.

    Order matters and is deliberate. Applicability is settled first, so a stale
    BLOCK is reported as stale rather than as a refusal that still stands — it
    was a refusal about an asset that has since changed, and the only honest
    thing to do with it is check again.
    """
    if not isinstance(status, Mapping):
        return _not_applicable(
            ReceiptState.INVALID, None, "receipt could not be read back"
        )

    verdict = status.get("verdict")
    if not verdict:
        return _not_applicable(ReceiptState.ABSENT, None, "no receipt on this asset")

    verdict = str(verdict)
    if verdict not in VERDICTS:
        return _not_applicable(
            ReceiptState.INVALID,
            verdict,
            f"receipt records an unrecognised verdict ({verdict})",
        )

    # Freshness must be positively established, not merely un-asserted. A status
    # that omits the marker, or carries something other than a bool where the
    # reader computed one, has not been proved current — and "we could not tell"
    # is unreadable, not fresh. A hostile or broken catalog payload therefore
    # cannot win coverage by leaving a field out.
    stale = status.get("stale")
    if stale is True:
        return _not_applicable(
            ReceiptState.STALE,
            verdict,
            f"receipt is stale: {status.get('stale_reason') or 'unknown'}",
        )
    if stale is not False:
        return _not_applicable(
            ReceiptState.INVALID,
            verdict,
            "receipt freshness could not be established",
        )

    if verdict == "BLOCK":
        refusal = status.get("reason_code") or "BLOCK"
        return ReceiptJudgment(
            state=ReceiptState.CURRENT,
            verdict=verdict,
            action=Action.STOP,
            covers=True,
            reason=f"receipt records a current refusal ({refusal}); stop",
        )
    if verdict == "WARN":
        return ReceiptJudgment(
            state=ReceiptState.CURRENT,
            verdict=verdict,
            action=Action.REVIEW,
            covers=True,
            reason="receipt records WARN; review or escalate before acting",
        )
    return ReceiptJudgment(
        state=ReceiptState.CURRENT,
        verdict=verdict,
        action=Action.CONTINUE,
        covers=True,
        reason="receipt records PASS; continue",
    )


def _not_applicable(
    state: ReceiptState, verdict: str | None, reason: str
) -> ReceiptJudgment:
    """A receipt that does not apply authorizes nothing and covers nothing.

    Both consequences are stated in one place so no caller can pick up the
    authorization half without the coverage half, which is how the two drifted
    apart before.
    """
    return ReceiptJudgment(
        state=state,
        verdict=verdict,
        action=Action.RECHECK,
        covers=False,
        reason=reason,
    )
