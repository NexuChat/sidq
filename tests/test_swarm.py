"""Cooperation with no coordinator: does it actually add up?

`sidq.agent.swarm` claims that several `SwarmWorker` processes, sharing
nothing but a DataHub catalog and no lock service, converge on full coverage
instead of either colliding pointlessly or leaving gaps a solo run would have
caught. Every test here is a way that claim could quietly be false: a
rotation that secretly drops or duplicates a target, a skip that cannot say
whose receipt it is trusting, a stale or refused receipt read as a pass, a
transport error read as a clean bill, a write failure that takes the whole
worker down with it, or an observer that credits a receipt to the wrong run.
None of these would show up by reading the module — they only show up by
making two workers actually share a catalog and watching what each one
decides.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock, Thread, current_thread

from sidq.agent.auditor import Target
from sidq.agent.swarm import SwarmWorker, WorkerRun, observe, rotate_for
from sidq.gates.self_contradiction import CatalogEntity, CatalogField, CatalogSnapshot
from sidq.models import Verdict
from sidq.policy.engine import PolicyEngine
from sidq.receipt.build import build_receipt
from sidq.receipt.read import get_verification_status
from sidq.receipt.write import write_receipt

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
POLICY_HASH = PolicyEngine().policy.policy_hash


def _dataset(name: str, **kwargs: object) -> CatalogEntity:
    return CatalogEntity(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.{name},PROD)",
        kind="dataset",
        fields=kwargs.pop("fields", (CatalogField("id"),)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class FakeDataHub:
    """An in-memory double for the three MCP tools a receipt write touches, plus
    the one a receipt read touches.

    `get_entities` returns exactly what an earlier `write_receipt` call stored
    for that URN through this same double — so a swarm's own writes are visible
    to the next read, the same as they would be through the real MCP server.
    Every call is kept in `calls` so a test can inspect what was actually asked
    of the catalog, not just the worker's own account of it.
    """

    def __init__(self) -> None:
        self._receipts: dict[str, dict[str, list[str]]] = {}
        self._tags: dict[str, set[str]] = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, arguments: dict) -> object:
        arguments = dict(arguments)
        self.calls.append((name, arguments))
        if name == "get_entities":
            return {"entities": [self._entity(urn) for urn in arguments["urns"]]}
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "returned": 0,
                    "hasMore": False,
                    "searchResults": [],
                }
            }
        if name == "save_document":
            return {"urn": "urn:li:document:fake-doc"}
        if name == "add_structured_properties":
            urn = arguments["entity_urns"][0]
            self._receipts.setdefault(urn, {}).update(arguments["property_values"])
            return {}
        if name == "remove_structured_properties":
            for urn in arguments["entity_urns"]:
                receipt = self._receipts.setdefault(urn, {})
                for property_urn in arguments["property_urns"]:
                    receipt.pop(property_urn, None)
            return {}
        if name == "add_tags":
            for urn in arguments["entity_urns"]:
                self._tags.setdefault(urn, set()).update(arguments["tag_urns"])
            return {}
        if name == "remove_tags":
            for urn in arguments["entity_urns"]:
                self._tags.setdefault(urn, set()).difference_update(
                    arguments["tag_urns"]
                )
            return {}
        raise AssertionError(f"FakeDataHub does not implement {name!r}")

    def _entity(self, urn: str) -> dict:
        return {
            "urn": urn,
            "datasetProperties": {"lastModified": "2026-08-02T10:00:00+00:00"},
            "globalTags": {
                "tags": [{"tag": {"urn": tag}} for tag in self._tags.get(urn, set())]
            },
            "structuredProperties": {
                "properties": [
                    {
                        "structuredProperty": {"urn": key},
                        "values": [{"stringValue": value} for value in values],
                    }
                    for key, values in self._receipts.get(urn, {}).items()
                ]
            },
        }


def _seed_receipt(
    hub: FakeDataHub,
    urn: str,
    *,
    decision: str = "PASS",
    swarm_run: str = "",
    worker_id: str = "",
    checked_at: datetime = NOW,
) -> None:
    """Write a receipt through the real build/write path, the way any auditor —
    swarm worker or solo run — actually produces one."""
    verdict = Verdict(
        decision=decision,
        reason_code=None,
        findings=(),
        touched=(),
        commit_sha="",
        policy_hash=POLICY_HASH,
    )
    receipt = build_receipt(
        urn, verdict, checked_at=checked_at, swarm_run=swarm_run, worker_id=worker_id
    )
    write_receipt(receipt, hub)


def _is_rotation(candidate: list[Target], original: list[Target]) -> bool:
    """Is `candidate` the same cyclic sequence as `original`, just entered
    somewhere else?"""
    if len(candidate) != len(original):
        return False
    doubled = list(original) * 2
    width = len(original)
    return any(
        doubled[start : start + width] == list(candidate)
        for start in range(len(original))
    )


# ---------------------------------------------------------------------------
# rotate_for: an entry point into the plan, never a partition of it.


def test_rotate_for_returns_the_same_order_for_the_same_worker_id() -> None:
    """A worker that restarts, or a transcript replayed for review, must
    retrace the exact same entry point — a fresh random start each call would
    make a swarm run irreproducible."""
    plan = [Target(f"urn:m{index}", index, ()) for index in range(6)]

    assert rotate_for("worker-a", plan) == rotate_for("worker-a", plan)


def test_rotate_for_shifts_the_starting_point_but_never_drops_a_target() -> None:
    """rotate_for only enters the plan somewhere else; if it silently dropped
    or duplicated a target, a worker with budget to spare could finish a
    shift without ever having looked at some asset."""
    plan = [Target(f"urn:m{index}", index, ()) for index in range(6)]

    for worker_id in ("worker-a", "worker-b", "worker-c", "worker-d"):
        rotated = rotate_for(worker_id, plan)
        assert set(rotated) == set(plan)
        assert _is_rotation(rotated, plan)


def test_different_worker_ids_start_at_different_offsets() -> None:
    """If every worker id rotated to the same offset, several workers would
    just be several copies racing down the identical order — the exact
    duplication a swarm exists to avoid."""
    plan = [Target(f"urn:m{index}", index, ()) for index in range(6)]

    starts = {rotate_for(f"worker-{index}", plan)[0] for index in range(8)}

    assert len(starts) > 1


# ---------------------------------------------------------------------------
# The skip: only when a receipt still holds, and never anonymously.


def test_a_worker_skips_an_asset_a_peers_receipt_still_vouches_for() -> None:
    """The skip has to say whose word is being trusted. Recording it as this
    worker's own clean bill, or as nobody's, would hide which peer's
    examination the report actually rests on."""
    shared = _dataset("shared", owners=("urn:li:corpuser:a",))
    other = _dataset("other", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()
    _seed_receipt(hub, shared.urn, swarm_run="swarm-1", worker_id="worker-peer")

    result = SwarmWorker(
        CatalogSnapshot((shared, other)),
        worker_id="worker-self",
        swarm_run="swarm-1",
        tool_caller=hub,
        budget=5,
        now=lambda: NOW,
    ).run()

    assert result.vouched_by_peer == [(shared.urn, "worker-peer")]
    assert shared.urn not in result.examined
    assert other.urn in result.examined


def test_a_worker_re_examines_a_receipt_that_records_block_or_has_gone_stale() -> None:
    """`holds()` already draws this line; a swarm worker that skipped on a
    refusal or a stale checked_at would be treating "we refused" or "we
    checked long ago under a different policy" as a current pass — quietly
    promoting a refusal into a clean bill."""
    blocked = _dataset("blocked", owners=("urn:li:corpuser:a",))
    stale = _dataset("stale", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()
    _seed_receipt(hub, blocked.urn, decision="BLOCK", checked_at=NOW)
    _seed_receipt(hub, stale.urn, decision="PASS", checked_at=NOW - timedelta(days=30))

    result = SwarmWorker(
        CatalogSnapshot((blocked, stale)),
        worker_id="worker-self",
        swarm_run="swarm-1",
        tool_caller=hub,
        budget=5,
        now=lambda: NOW,
    ).run()

    assert set(result.examined) == {blocked.urn, stale.urn}
    assert result.vouched_by_peer == []
    assert result.vouched_unattributed == []


# ---------------------------------------------------------------------------
# Convergence: the property the whole design exists to buy.


def test_two_workers_in_sequence_converge_and_the_second_does_less_new_work() -> None:
    """Budgets are per worker, not shared, so a second worker running after the
    first must find its own receipts and let them shrink its work — not
    repeat the whole plan and call that cooperation."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(4)
    )
    hub = FakeDataHub()

    first = SwarmWorker(
        CatalogSnapshot(entities),
        worker_id="worker-1",
        swarm_run="swarm-x",
        tool_caller=hub,
        budget=3,
        now=lambda: NOW,
    ).run()
    assert len(first.examined) == 3

    second = SwarmWorker(
        CatalogSnapshot(entities),
        worker_id="worker-2",
        swarm_run="swarm-x",
        tool_caller=hub,
        budget=3,
        now=lambda: NOW,
    ).run()

    # Whatever the first worker did not reach is the only thing left to find.
    assert len(second.examined) == 1
    assert len(second.examined) < len(first.examined)
    assert len(second.vouched_by_peer) == 3
    assert {peer for _, peer in second.vouched_by_peer} == {"worker-1"}

    all_urns = {entity.urn for entity in entities}
    assert set(first.examined) | set(second.examined) == all_urns
    assert set(first.examined) != all_urns
    assert set(second.examined) != all_urns


# ---------------------------------------------------------------------------
# Attribution: swarm receipts carry it, solo receipts must not fake it.


def test_every_receipt_a_swarm_worker_writes_carries_its_run_and_worker_id() -> None:
    """Attribution is the entire point of a swarm receipt — a receipt with no
    worker_id looks, to a peer reading it back, exactly like an ordinary solo
    run's, and `vouched_by_peer` could never be filled in."""
    entity = _dataset("attributed", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()

    SwarmWorker(
        CatalogSnapshot((entity,)),
        worker_id="worker-mark",
        swarm_run="swarm-42",
        tool_caller=hub,
        budget=1,
        now=lambda: NOW,
    ).run()

    status = get_verification_status(
        entity.urn, hub, current_policy_hash=POLICY_HASH, now=NOW
    )
    assert status["swarm_run"] == "swarm-42"
    assert status["worker_id"] == "worker-mark"


def test_a_solo_receipt_carries_neither_swarm_run_nor_worker_id() -> None:
    """The absence has to be a true absence, not an empty string a reader could
    mistake for "ran solo under swarm run ''". Writing a blank value would
    claim cooperation that never happened."""
    verdict = Verdict(
        decision="PASS",
        reason_code=None,
        findings=(),
        touched=(),
        commit_sha="abc",
        policy_hash=POLICY_HASH,
    )
    receipt = build_receipt(
        "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.solo,PROD)", verdict
    )

    values = receipt.structured_property_values()

    assert "urn:li:structuredProperty:sidq.swarm_run" not in values
    assert "urn:li:structuredProperty:sidq.worker_id" not in values


# ---------------------------------------------------------------------------
# Failure handling: a transport hiccup must never read as "clean", and a
# rejected write must never stop the shift.


def test_a_read_failure_is_treated_as_unknown_not_as_clean() -> None:
    """`_read_status` already turns a transport exception into "unknown"; the
    failure this guards against is the worker instead reading its own `None`
    as "no receipt, so nothing to worry about", which would let a network
    blip excuse a whole asset from the audit."""
    entity = _dataset("flaky", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()

    def failing_reads(name: str, arguments: dict) -> object:
        if name == "get_entities":
            raise ConnectionError("mcp transport reset")
        return hub(name, arguments)

    result = SwarmWorker(
        CatalogSnapshot((entity,)),
        worker_id="worker-self",
        swarm_run="swarm-1",
        tool_caller=failing_reads,
        budget=1,
        now=lambda: NOW,
    ).run()

    assert result.examined == [entity.urn]
    assert result.vouched_by_peer == []
    assert result.vouched_unattributed == []
    # A write cannot mint a fresh receipt when its own decision context read fails.
    status = get_verification_status(
        entity.urn, hub, current_policy_hash=POLICY_HASH, now=NOW
    )
    assert status["verdict"] is None
    assert result.write_failures == [(entity.urn, "ConnectionError")]


def test_a_write_failure_is_recorded_and_the_run_continues() -> None:
    """One rejected mutation must not read as "nothing was examined this
    shift"; it has to show up as a named failure while every other target
    still gets its receipt."""
    rejected = _dataset("rejected", owners=("urn:li:corpuser:a",))
    fine = _dataset("fine", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()

    def failing_write(name: str, arguments: dict) -> object:
        if name == "add_structured_properties" and arguments.get("entity_urns") == [
            rejected.urn
        ]:
            raise RuntimeError("datahub rejected the mutation")
        return hub(name, arguments)

    result = SwarmWorker(
        CatalogSnapshot((rejected, fine)),
        worker_id="worker-self",
        swarm_run="swarm-1",
        tool_caller=failing_write,
        budget=2,
        now=lambda: NOW,
    ).run()

    assert result.write_failures == [(rejected.urn, "RuntimeError")]
    assert rejected.urn not in result.written
    assert fine.urn in result.written
    assert set(result.examined) == {rejected.urn, fine.urn}


# ---------------------------------------------------------------------------
# observe(): the ledger read from the catalog, not from what a worker claims.


def test_observe_attributes_current_receipts_and_counts_block_as_examined() -> None:
    """The observer is a separate process by design, so it must earn every
    number from the receipts alone: crediting a receipt to the wrong worker,
    or counting an older swarm run's receipts as this run's coverage, would
    let a swarm's report claim work nobody did this shift."""
    urns = ["urn:a", "urn:b", "urn:c", "urn:d"]
    statuses = {
        "urn:a": {
            "verdict": "PASS",
            "stale": False,
            "swarm_run": "swarm-2",
            "worker_id": "worker-1",
        },
        "urn:b": {
            "verdict": "PASS",
            "stale": False,
            "swarm_run": "swarm-2",
            "worker_id": "worker-2",
        },
        # A receipt from an earlier swarm run: still counts as current, but
        # must not be credited to this run's per-worker tally.
        "urn:c": {
            "verdict": "PASS",
            "stale": False,
            "swarm_run": "swarm-1",
            "worker_id": "worker-1",
        },
        # A current refusal is still evidence that this worker examined the asset.
        "urn:d": {
            "verdict": "BLOCK",
            "stale": False,
            "swarm_run": "swarm-2",
            "worker_id": "worker-2",
        },
    }

    report = observe(urns, statuses, swarm_run="swarm-2")

    assert report.total_assets == 4
    assert report.current_receipts == 4
    assert report.by_worker == {"worker-1": 1, "worker-2": 2}


def test_observe_counts_only_current_valid_receipts() -> None:
    report = observe(
        ["urn:holding", "urn:stale", "urn:blocked"],
        {
            "urn:holding": {
                "verdict": "PASS",
                "stale": False,
                "swarm_run": "swarm-2",
                "worker_id": "worker-1",
            },
            "urn:stale": {
                "verdict": "PASS",
                "stale": True,
                "stale_reason": "metadata changed",
                "swarm_run": "swarm-2",
                "worker_id": "worker-2",
            },
            "urn:blocked": {
                "verdict": "BLOCK",
                "stale": False,
                "swarm_run": "swarm-2",
                "worker_id": "worker-3",
            },
        },
        swarm_run="swarm-2",
    )

    assert report.current_receipts == 2
    assert report.by_worker == {"worker-1": 1, "worker-3": 1}


def test_observe_cannot_certify_freshness_when_stale_marker_is_missing_or_malformed() -> (
    None
):
    report = observe(
        ["urn:missing", "urn:malformed"],
        {
            "urn:missing": {"verdict": "PASS"},
            "urn:malformed": {"verdict": "PASS", "stale": "false"},
        },
        swarm_run="swarm-2",
    )

    assert report.current_receipts == 0


def test_observe_render_contains_only_current_catalog_facts() -> None:
    report = observe(
        ["urn:a"],
        {
            "urn:a": {
                "verdict": "PASS",
                "stale": False,
                "swarm_run": "swarm-2",
                "worker_id": "worker-1",
            }
        },
        swarm_run="swarm-2",
    )

    lines = "\n".join(report.render())
    summary = report.summary()

    assert "current valid receipts  1 of 1" in lines
    assert "receipted before" not in lines
    assert "duplicate" not in lines
    assert "recovered" not in lines
    assert "receipted_before" not in summary
    assert "duplicates" not in summary
    assert "recovered" not in summary


def test_interleaved_workers_can_both_examine_but_latest_receipt_cannot_prove_duplicate() -> (
    None
):
    entity = _dataset("collision", owners=("urn:li:corpuser:a",))
    hub = FakeDataHub()
    barrier = Barrier(2)
    lock = Lock()
    initial_readers: set[str] = set()
    first_confirmation = Event()
    writes_started = 0

    def colliding_hub(name: str, arguments: dict) -> object:
        nonlocal writes_started
        caller = current_thread().name
        if name == "get_entities" and caller.startswith("sidq-swarm-test-"):
            with lock:
                is_initial = caller not in initial_readers
                if is_initial:
                    initial_readers.add(caller)
                    response = {
                        "entities": [hub._entity(urn) for urn in arguments["urns"]]
                    }
            if is_initial:
                barrier.wait()
                return response
        if name == "add_structured_properties":
            with lock:
                writes_started += 1
                waits_for_first_confirmation = writes_started == 2
            if waits_for_first_confirmation:
                assert first_confirmation.wait(timeout=5)
        response = hub(name, arguments)
        if name == "get_entities" and caller == "sidq-receipt-confirmation":
            first_confirmation.set()
        return response

    runs: dict[str, WorkerRun] = {}

    def work(worker: str) -> None:
        runs[worker] = SwarmWorker(
            CatalogSnapshot((entity,)),
            worker_id=worker,
            swarm_run="swarm-collision",
            tool_caller=colliding_hub,
            budget=1,
            now=lambda: NOW,
        ).run()

    threads = [
        Thread(target=work, args=(worker,), name=f"sidq-swarm-test-{worker}")
        for worker in ("one", "two")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert initial_readers == {"sidq-swarm-test-one", "sidq-swarm-test-two"}
    assert first_confirmation.is_set()
    assert all(run.examined == [entity.urn] for run in runs.values())
    assert all(run.written == [entity.urn] for run in runs.values())

    status = get_verification_status(
        entity.urn, hub, current_policy_hash=POLICY_HASH, now=NOW
    )
    report = observe([entity.urn], {entity.urn: status}, swarm_run="swarm-collision")
    assert report.current_receipts == 1
    assert sum(report.by_worker.values()) == 1
