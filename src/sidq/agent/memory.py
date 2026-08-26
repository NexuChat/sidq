"""What the catalog already remembers about its own verification.

The auditor's budget is explicit, and until now it was also amnesiac: every run
started from zero, so a catalog larger than one budget could never be fully
covered — the same worst assets were re-examined forever and the tail was
deferred forever. The state needed to fix that already exists, and it does not
live in a file beside the agent. It lives in the catalog, as the receipts a
previous run wrote back.

`recall` reads those receipts and answers one question per asset: **has this
asset already been examined under conditions that still apply?** That is the
coverage axis of `judge()` — the same judgment every other receipt consumer
uses — recomputed by this reader, never trusted from storage. A covered asset
can be skipped without loss, and the reason is worth stating precisely: the
engine is deterministic, so a receipt is a memoised verdict keyed by the
asset's content and the policy hash. Staleness is the cache invalidation —
asset changed, receipt aged out, policy changed — and anything stale, absent,
or unreadable goes back in the queue.

Coverage is deliberately not authorization. A current BLOCK covers its asset:
the engine looked, and it refused. Re-deriving that refusal every run is how
the resume path used to starve, spending the whole budget re-refusing the same
assets while the untouched tail was never reached. What a BLOCK does *not* do
is authorize anything — `PriorReceipt.action` carries that separately, and the
report renders a refusal as a refusal rather than as a vouch.

Two properties follow that matter more than the budget arithmetic.

**Coverage converges.** Run after run, the vouched set grows and the budget
flows to assets never yet examined, until the whole catalog is covered — under
a budget that never changed.

**The memory is shared without coordination.** Any sidq instance pointed at
the same catalog reads the same receipts, so one agent resumes where another
stopped. There is no separate state store or server to sync; the catalog holds
shared current verification memory.

Failure is closed in the right direction: if the receipts cannot be read, the
prior is empty and everything is re-examined. Forgetting costs budget;
trusting a receipt that could not be read would cost the thesis.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sidq.receipt.read import ToolCaller, get_verification_statuses
from sidq.receipt.state import Action, ReceiptState, judge


@dataclass(frozen=True, slots=True)
class PriorReceipt:
    """One asset's remembered verdict, and this reader's judgment of it.

    The three axes stay separate all the way to the caller. `covers` answers
    "was it examined"; `action` answers "what may be done about it"; `state`
    answers "does the receipt still apply". A planner that only wants to know
    whether to spend budget reads `covers` and nothing else.
    """

    urn: str
    covers: bool
    reason: str
    state: ReceiptState = ReceiptState.ABSENT
    verdict: str | None = None
    action: Action = Action.RECHECK
    # Which worker wrote the receipt, when a swarm wrote it. Empty otherwise —
    # a solo run has nobody to credit, and inventing an id would be a lie.
    worker_id: str = ""

    @property
    def refused(self) -> bool:
        """A current receipt recording a refusal: covered, but never a vouch."""
        return self.action is Action.STOP

    @property
    def may_continue(self) -> bool:
        """Whether this receipt authorizes acting — true only for a current PASS.

        Deliberately not the same question as `covers`. Every caller that used
        to ask one boolean now has to say which of the two it meant.
        """
        return self.action is Action.CONTINUE


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
        judgment = judge(status)
        prior[urn] = PriorReceipt(
            urn=urn,
            covers=judgment.covers,
            reason=judgment.reason,
            state=judgment.state,
            verdict=judgment.verdict,
            action=judgment.action,
            worker_id=str(status.get("worker_id") or ""),
        )
    return prior


# -- the other half: what a previous run could NOT establish --------------------

_GAP_BATCH = 25


def standing_gaps(urns: Sequence[str], transport: Any) -> frozenset[str]:
    """Which of these assets a previous run examined and established nothing on.

    `recall` above reads back the receipts a run *earned* and skips those assets.
    This reads back the coverage gaps a run *left* — the `ERROR` assertions
    `emit_gap_assertions` writes for an examined asset that yielded nothing — and
    hands them to the planner as a tie-break, never as a demotion.

    The asymmetry it fixes is worth naming. An agent that remembers only its
    successes spends every budget re-examining the same assets that already
    resisted it, while the assets no run has ever touched wait forever. Coverage
    converges on the successes and stalls on the tail. Remembering the scars is
    what lets the budget move past them without abandoning them: consequence
    still decides who goes first, and this only settles who goes first among
    equals.

    A gap URN is derived, not stored: `assertion_urn(asset, "not_established")`
    is deterministic, so this asks the catalog about addresses it computed rather
    than about a list it has to keep somewhere and keep honest.

    Anything unreadable is simply not a gap. A transport failure must not invent
    scars — the honest failure here is to plan as though nothing had ever been
    tried, which is the order this agent used before any of this existed.
    """
    from sidq.receipt.assertion import _GAP_RULE_ID, assertion_urn

    wanted = {assertion_urn(urn, _GAP_RULE_ID): urn for urn in dict.fromkeys(urns)}
    if not wanted:
        return frozenset()

    standing: set[str] = set()
    addresses = list(wanted)
    for start in range(0, len(addresses), _GAP_BATCH):
        chunk = addresses[start : start + _GAP_BATCH]
        aliases = "\n".join(
            f'a{index}: assertion(urn: "{address}") '
            "{ runEvents(limit: 1) { runEvents { result { type } } } }"
            for index, address in enumerate(chunk)
        )
        try:
            data = transport.graphql("{ " + aliases + " }", {})
        except Exception as error:  # noqa: BLE001 - transports raise several types
            # Not silence. Planning as though nothing was ever tried is the safe
            # failure, but an operator who is never told will read the resulting
            # order as a decision rather than as a degraded one.
            print(
                f"sidq: could not read coverage gaps for {len(chunk)} asset(s): "
                f"{type(error).__name__}",
                file=sys.stderr,
            )
            continue
        if not isinstance(data, Mapping):
            continue
        for index, address in enumerate(chunk):
            node = data.get(f"a{index}")
            if not isinstance(node, Mapping):
                continue
            events = (node.get("runEvents") or {}).get("runEvents") or []
            latest = events[0] if events else None
            if not isinstance(latest, Mapping):
                continue
            if ((latest.get("result") or {}).get("type")) == "ERROR":
                standing.add(wanted[address])
    return frozenset(standing)
