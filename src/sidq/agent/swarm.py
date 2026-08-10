"""Many auditors, one catalog, no coordinator.

`sidq.agent.memory` made a *single* audit resumable: receipts written by an
earlier run tell a later one what it can skip. That is cooperation across time.
This is the same trick across *space* — several auditor processes working the
same catalog at the same moment, with no message bus, no lock service, no
leader, and no shared filesystem between them. The only thing they share is
DataHub.

**Why the resumable auditor could not simply be run four times.** It recalls
every receipt once at the start, plans, and writes at the end. Two copies
launched together would therefore read the same empty prior, choose the same
worst-first order, and re-examine the same assets — cooperation in the report
and duplication in the work. The honest version streams: before each target it
re-reads *that asset's* receipt, and after each verdict it writes immediately.
The window in which two workers can collide shrinks from a whole run to the
duration of one examination.

**What is promised, precisely.** At-least-once, never exactly-once. MCP offers
no claim or compare-and-set primitive. Per-target digest ordering removes the
modulus collision that made two rotations share one cyclic path, but it cannot
exclude collisions. Two orders can share a next target, a prefix, or — by full-
permutation coincidence — an entire consequence tier. Workers that reach the
same unreceipted asset before either receipt becomes visible will both examine
it. That is safe — the engine is deterministic, so both write the same verdict.
DataHub exposes the latest receipt, not an append-only examination history, so
the observer does not claim it can count collisions after the fact.

**Why the work splits at all.** The exact consequence ranking is cut into
bounded tiers, then each worker independently orders a tier by a digest of its
worker id and the target URN. Real consequence scores are usually distinct; an
older version shuffled only exact ties and therefore gave every worker the same
plan. A worker now gives up exact rank only inside a small high-consequence
neighbourhood, exhausts that neighbourhood before the next, and still walks the
whole plan. No negotiation: the different early paths do useful work before a
receipt can possibly arrive, and receipts narrow the remaining overlap later.

**And if a worker dies mid-run**, its unfinished assets are left unreceipted —
so the survivors, re-reading before every target, pick them up as ordinary
work. Nothing is stranded, because nothing was ever assigned.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sidq.agent.auditor import CatalogAuditor, Target
from sidq.gates.self_contradiction import CatalogSnapshot, SelfContradictionGate
from sidq.models import Evidence
from sidq.policy.engine import PolicyEngine
from sidq.receipt.build import build_receipt
from sidq.receipt.read import ToolCaller, get_verification_status
from sidq.receipt.state import judge

_CONSEQUENCE_TIER_SIZE = 16


@dataclass
class WorkerRun:
    """One worker's account of its own shift — and of whose word it took."""

    worker_id: str
    swarm_run: str
    examined: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    findings: list[Evidence] = field(default_factory=list)
    # (urn, worker_id) — an asset skipped because a peer's current PASS/WARN
    # receipt already covers it.
    vouched_by_peer: list[tuple[str, str]] = field(default_factory=list)
    # Skipped on a receipt with no worker id: an earlier solo run, or this one.
    vouched_unattributed: list[str] = field(default_factory=list)
    # Skipped because a current receipt records a refusal. Covered, so the
    # worker moves on rather than re-refusing it every pass — but kept apart
    # from the vouched lists, because a refusal is not a peer's approval.
    refused: list[str] = field(default_factory=list)
    write_failures: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "swarm_run": self.swarm_run,
            "examined": len(self.examined),
            "receipts_written": len(self.written),
            "findings": len(self.findings),
            "vouched_by_peer": len(self.vouched_by_peer),
            "vouched_unattributed": len(self.vouched_unattributed),
            "blocked_by_receipt": len(self.refused),
            "write_failures": len(self.write_failures),
        }


def order_for_worker(worker_id: str, plan: Sequence[Target]) -> list[Target]:
    """Order bounded consequence tiers by a worker-seeded target digest.

    The plan is ranked exactly first, then cut into nominal sixteen-target
    tiers. An exact tie is never split, and zero-consequence work never shares
    a tier with positive-consequence work. A worker can therefore examine the
    sixteenth-ranked positive target before the first, but it cannot fall into
    the next tier while this one remains. That bounded loss of exact rank is the
    deliberate price of making distinct real-world scores spread before slow
    receipts exist; shuffling exact ties did nothing when every band was one.

    Within a tier, hashing the worker id with every URN derives a separate,
    deterministic key instead of an offset into one cyclic path. This is not a
    partition: every worker still receives the whole plan, so no asset belongs
    to one worker and a dead worker strands nothing. It is also not a claim:
    workers can still share a target or, by permutation coincidence, a prefix.
    """
    seed = worker_id.encode() + b"\0"
    ordered: list[Target] = []
    for tier in _consequence_tiers(plan):
        ordered.extend(
            sorted(
                tier,
                key=lambda target: (
                    hashlib.sha256(seed + target.urn.encode()).digest(),
                    target.urn,
                    -target.consequence,
                    target.reasons,
                ),
            )
        )
    return ordered


def _consequence_tiers(plan: Sequence[Target]) -> list[list[Target]]:
    """Make bounded rank tiers without mixing notable and trivial exposure.

    A tier is closed at a score boundary, never inside one: splitting a tie
    would rank two identically-consequential assets against each other on
    nothing. The size is therefore a target rather than a hard cap, and one
    tie run larger than the target is unavoidably its own oversized tier.

    What is NOT allowed is that run absorbing the assets ranked above it. An
    earlier version closed a tier only once it was already full, so fifteen
    distinct high scores followed by four hundred assets tied at fifteen — the
    ordinary shape of a catalog, where every unowned leaf table scores exactly
    fifteen — produced a single tier of four hundred and fifteen, and a worker
    with a budget of six could spend its whole shift at consequence fifteen
    while the most consequential asset in the catalog sat three hundred places
    away, unexamined. Each score's whole run is now measured before it is
    admitted, so a run that would overflow the tier starts a new one instead.
    """
    ranked = sorted(
        plan,
        key=lambda target: (-target.consequence, target.urn, target.reasons),
    )
    runs: list[list[Target]] = []
    for target in ranked:
        if runs and runs[-1][0].consequence == target.consequence:
            runs[-1].append(target)
        else:
            runs.append([target])

    tiers: list[list[Target]] = []
    tier: list[Target] = []
    for run in runs:
        crosses_zero = bool(tier) and (tier[0].consequence > 0) != (
            run[0].consequence > 0
        )
        would_overflow = bool(tier) and len(tier) + len(run) > _CONSEQUENCE_TIER_SIZE
        if crosses_zero or would_overflow:
            tiers.append(tier)
            tier = []
        tier.extend(run)
    if tier:
        tiers.append(tier)
    return tiers


class SwarmWorker:
    """One auditor in a swarm: read fresh, decide, write now, move on.

    The differences from `CatalogAuditor.run` are three, and each exists because
    peers are working the same catalog at the same time: the receipt is read per
    target instead of once per run, the receipt is written per verdict instead
    of once per run, and bounded consequence tiers receive a worker-specific
    order.
    """

    def __init__(
        self,
        snapshot: CatalogSnapshot,
        *,
        worker_id: str,
        swarm_run: str,
        tool_caller: ToolCaller,
        budget: int,
        policy_path: str | None = None,
        commit_sha: str = "",
        max_age: timedelta = timedelta(days=7),
        now: Callable[[], datetime] | None = None,
        gate: SelfContradictionGate | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._worker_id = worker_id
        self._swarm_run = swarm_run
        self._caller = tool_caller
        self._budget = max(0, budget)
        self._engine = PolicyEngine(policy_path)
        self._commit_sha = commit_sha
        self._max_age = max_age
        self._now = now
        self._auditor = CatalogAuditor(snapshot, budget=budget, gate=gate)

    def run(self) -> WorkerRun:
        result = WorkerRun(worker_id=self._worker_id, swarm_run=self._swarm_run)
        policy_hash = self._engine.decide((), commit_sha="").policy_hash

        for target in order_for_worker(self._worker_id, self._auditor.plan()):
            if len(result.examined) >= self._budget:
                break

            # Fresh, per-target read. This is the whole difference between a
            # swarm and four solo runs racing each other.
            status = self._read_status(target.urn, policy_hash)
            if status is not None:
                # Coverage, not authorization: another worker having already
                # examined this asset is what lets this one move on, whatever
                # the verdict was. A refusal is recorded separately so the
                # worker's account never reads as a peer vouching for it.
                judgment = judge(status)
                if judgment.covers:
                    if judgment.refused:
                        result.refused.append(target.urn)
                        continue
                    peer = str(status.get("worker_id") or "")
                    if peer and peer != self._worker_id:
                        result.vouched_by_peer.append((target.urn, peer))
                    else:
                        result.vouched_unattributed.append(target.urn)
                    continue

            # Same package, deliberate: the swarm reuses the solo auditor's
            # examination verbatim so both paths cannot drift apart.
            evidence = self._auditor._examine(target)
            result.examined.append(target.urn)
            found = [
                item for item in evidence if not item.kind.endswith("_unverifiable")
            ]
            result.findings.extend(found)

            # Examined but nothing establishable gets no receipt — the same rule
            # the solo auditor follows, for the same reason.
            if not found and any(
                item.kind.endswith("_unverifiable") for item in evidence
            ):
                continue
            self._write(target.urn, tuple(evidence), result)
        return result

    def _read_status(self, urn: str, policy_hash: str) -> dict[str, object] | None:
        """Read one receipt; a transport failure means 'unknown', never 'clean'."""
        try:
            return get_verification_status(
                urn,
                self._caller,
                current_policy_hash=policy_hash,
                max_age=self._max_age,
                now=self._now() if self._now else None,
            )
        except Exception:  # noqa: BLE001 - MCP transports raise several types
            return None

    def _write(self, urn: str, evidence: tuple[Evidence, ...], run: WorkerRun) -> None:
        verdict = self._engine.decide(evidence, commit_sha=self._commit_sha)
        receipt = build_receipt(
            urn,
            verdict,
            checked_at=self._now() if self._now else None,
            swarm_run=self._swarm_run,
            worker_id=self._worker_id,
        )
        try:
            from sidq.receipt.write import write_receipt

            write_receipt(receipt, self._caller)
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            run.write_failures.append((urn, type(error).__name__))
            return
        run.written.append(urn)


def render_worker(run: WorkerRun) -> list[str]:
    """One worker's shift, said plainly — including whose word it took."""
    summary = run.summary()
    lines = [
        f"Swarm worker {run.worker_id} — run {run.swarm_run}",
        "",
        f"  examined          {summary['examined']}",
        f"  receipts written  {summary['receipts_written']}",
        f"  findings          {summary['findings']}",
    ]
    if run.vouched_by_peer:
        peers = sorted({peer for _, peer in run.vouched_by_peer})
        lines.append(
            f"  vouched by peers  {len(run.vouched_by_peer)} "
            f"({', '.join(peers)}) — their receipts held, so this worker moved on"
        )
    if run.vouched_unattributed:
        lines.append(
            f"  vouched (earlier) {len(run.vouched_unattributed)} "
            "(a receipt from before this swarm still covers them)"
        )
    if run.refused:
        lines.append(
            f"  BLOCKED           {len(run.refused)} "
            "(a current receipt records a refusal; covered, not approved)"
        )
    if run.write_failures:
        lines.append(f"  write failures    {len(run.write_failures)}")
    return lines


@dataclass(frozen=True, slots=True)
class SwarmReport:
    """What an observer can prove by reading DataHub alone."""

    swarm_run: str
    total_assets: int
    current_receipts: int
    by_worker: dict[str, int]

    def summary(self) -> dict[str, object]:
        return {
            "swarm_run": self.swarm_run,
            "total_assets": self.total_assets,
            "current_receipts": self.current_receipts,
            "current_run_receipts": sum(self.by_worker.values()),
            "by_worker": dict(sorted(self.by_worker.items())),
        }

    def render(self) -> list[str]:
        lines = [
            f"Swarm ledger — run {self.swarm_run or '(none)'}",
            "",
            f"  assets in catalog        {self.total_assets}",
            f"  current valid receipts  {self.current_receipts} of {self.total_assets}",
            f"  from this swarm run     {sum(self.by_worker.values())}",
            "",
            "Current receipts from this run, by worker:",
        ]
        for worker, count in sorted(self.by_worker.items()):
            lines.append(f"  {worker:<24} {count}")
        return lines


def observe(
    urns: Sequence[str],
    statuses: Mapping[str, Mapping[str, object]],
    *,
    swarm_run: str,
) -> SwarmReport:
    """Build the ledger from receipts alone — no worker is asked what it did.

    The observer is a separate process by design: a swarm that reported its own
    success would be exactly the self-attestation this project refuses.
    """
    by_worker: dict[str, int] = {}
    current_receipts = 0
    for urn in urns:
        status = statuses.get(urn) or {}
        if not _is_current_receipt(status):
            continue
        current_receipts += 1
        if str(status.get("swarm_run") or "") != swarm_run:
            continue
        worker = str(status.get("worker_id") or "unattributed")
        by_worker[worker] = by_worker.get(worker, 0) + 1
    return SwarmReport(
        swarm_run=swarm_run,
        total_assets=len(urns),
        current_receipts=current_receipts,
        by_worker=by_worker,
    )


def _is_current_receipt(status: Mapping[str, object]) -> bool:
    """The ledger counts coverage, so it asks the one judgment every reader uses.

    Counting a current BLOCK here is deliberate: the ledger reports which assets
    the swarm has *examined*, and a refusal is an examination. It is the same
    coverage axis the workers skip on, from the same function, so the two can
    never drift into disagreeing about what the run actually covered.
    """
    return judge(status).covers
