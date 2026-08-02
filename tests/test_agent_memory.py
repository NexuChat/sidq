"""The auditor's memory: does coverage converge, and is a skip ever dishonest?

The property under test is the one the module docstring claims: a receipt that
still holds is a memoised verdict, so skipping it loses nothing — and everything
else about the skip must stay visible. A vouched asset that showed up as
"verified clean", or received a fresh receipt it did not earn, would be the
agent taking credit for work a previous run did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sidq.agent import CatalogAuditor, PriorReceipt, recall, receipts_for
from sidq.agent.auditor import render
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)
from sidq.receipt.read import decision_context_hash

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _dataset(name: str, **kwargs: object) -> CatalogEntity:
    return CatalogEntity(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.{name},PROD)",
        kind="dataset",
        fields=kwargs.pop("fields", (CatalogField("id"),)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _vouches(urn: str) -> PriorReceipt:
    return PriorReceipt(urn=urn, holds=True, reason="receipt records PASS")


# ---------------------------------------------------------------------------
# The skip itself: budget flows past vouched assets, and the report says so.


def test_a_holding_receipt_frees_the_budget_for_an_unseen_asset() -> None:
    """Without memory, budget 1 re-examines the worst asset forever."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(3)
    )
    prior = {entities[0].urn: _vouches(entities[0].urn)}

    amnesiac = CatalogAuditor(CatalogSnapshot(entities), budget=1).run()
    remembering = CatalogAuditor(CatalogSnapshot(entities), budget=1, prior=prior).run()

    assert amnesiac.examined != remembering.examined
    assert entities[0].urn not in remembering.examined
    assert remembering.covered == 1
    assert remembering.vouched == [(entities[0].urn, "receipt records PASS")]


def test_a_vouched_asset_is_never_reported_as_verified_this_run() -> None:
    """'The receipt vouches' and 'this run verified' are different claims."""
    entities = (_dataset("only", owners=("urn:li:corpuser:a",)),)
    prior = {entities[0].urn: _vouches(entities[0].urn)}

    result = CatalogAuditor(CatalogSnapshot(entities), budget=5, prior=prior).run()

    assert result.verified == []
    assert result.examined == []
    assert result.summary()["vouched_by_receipt"] == 1
    assert "vouched         1" in "\n".join(render(result, catalog="test"))


def test_a_vouched_asset_earns_no_fresh_receipt() -> None:
    """Re-stamping an unexamined asset would launder a skip into evidence."""
    entities = (_dataset("only", owners=("urn:li:corpuser:a",)),)
    prior = {entities[0].urn: _vouches(entities[0].urn)}

    result = CatalogAuditor(CatalogSnapshot(entities), budget=5, prior=prior).run()

    assert receipts_for(result, commit_sha="abc") == []


def test_a_receipt_that_does_not_hold_is_re_examined() -> None:
    """Stale, blocked, and absent receipts are the same answer: look again."""
    entities = (_dataset("only", owners=("urn:li:corpuser:a",)),)
    prior = {
        entities[0].urn: PriorReceipt(
            urn=entities[0].urn,
            holds=False,
            reason="receipt is stale: asset schema changed after the last verification",
        )
    }

    result = CatalogAuditor(CatalogSnapshot(entities), budget=5, prior=prior).run()

    assert result.examined == [entities[0].urn]
    assert result.vouched == []


def test_a_promotion_does_not_pierce_a_holding_receipt() -> None:
    """A neighbour's lie is evidence about the neighbour, not a change here."""
    liar = _dataset("liar", owners=("urn:li:corpuser:a",))
    neighbour = _dataset("neighbour", owners=("urn:li:corpuser:a",))
    # A field-lineage edge into a field the neighbour does not have: contagious.
    edges = (LineageEdge(liar.urn, "id", neighbour.urn, "no_such_field"),)
    prior = {neighbour.urn: _vouches(neighbour.urn)}

    result = CatalogAuditor(
        CatalogSnapshot((liar, neighbour), edges), budget=5, prior=prior
    ).run()

    assert neighbour.urn not in result.examined
    assert (neighbour.urn, "receipt records PASS") in result.vouched


def test_memory_leaves_the_run_deterministic() -> None:
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(6)
    )
    prior = {entities[2].urn: _vouches(entities[2].urn)}

    first = CatalogAuditor(CatalogSnapshot(entities), budget=3, prior=prior).run()
    second = CatalogAuditor(CatalogSnapshot(entities), budget=3, prior=prior).run()

    assert first.examined == second.examined
    assert first.vouched == second.vouched
    assert first.deferred == second.deferred


# ---------------------------------------------------------------------------
# Convergence: the property the memory exists to buy.


def test_two_bounded_runs_cover_what_one_could_not() -> None:
    """Same catalog, same budget: run two picks up exactly where run one stopped."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(4)
    )
    snapshot = CatalogSnapshot(entities)

    first = CatalogAuditor(snapshot, budget=2).run()
    assert len(first.deferred) == 2

    # Run one wrote receipts for what it verified; run two reads them back.
    prior = {urn: _vouches(urn) for urn in first.verified}
    second = CatalogAuditor(snapshot, budget=2, prior=prior).run()

    assert set(second.examined) == {target.urn for target in first.deferred}
    assert second.deferred == []
    assert set(first.examined) | set(second.examined) == {
        entity.urn for entity in entities
    }


# ---------------------------------------------------------------------------
# `recall`: reading the memory back out of the catalog, in batches.


def _receipt_entity(urn: str, verdict: str, *, policy_hash: str = "sha256:p") -> dict:
    def prop(name: str, value: str) -> dict:
        return {
            "structuredProperty": {"urn": f"urn:li:structuredProperty:sidq.{name}"},
            "values": [{"stringValue": value}],
        }

    entity = {
        "urn": urn,
        "datasetProperties": {"lastModified": "2026-08-02T10:00:00+00:00"},
        "structuredProperties": {
            "properties": [
                prop("verdict", verdict),
                prop("checked_at", "2026-08-02T11:00:00Z"),
                prop("policy_hash", policy_hash),
            ]
        },
    }
    context_hash = decision_context_hash(urn, entity, _empty_lineage)
    entity["structuredProperties"]["properties"].append(
        prop("context_hash", context_hash)
    )
    return entity


def _empty_lineage(name: str, arguments: dict) -> dict:
    assert name == "get_lineage"
    direction = "upstreams" if arguments["upstream"] else "downstreams"
    return {
        direction: {
            "total": 0,
            "returned": 0,
            "hasMore": False,
            "searchResults": [],
        }
    }


def test_recall_judges_each_receipt_now_rather_than_trusting_it() -> None:
    urns = [_dataset(name).urn for name in ("passed", "blocked", "unwritten")]
    entities = {
        urns[0]: _receipt_entity(urns[0], "PASS"),
        urns[1]: _receipt_entity(urns[1], "BLOCK"),
    }

    def caller(name: str, arguments: dict) -> dict:
        if name == "get_lineage":
            return _empty_lineage(name, arguments)
        found = [entities[urn] for urn in arguments["urns"] if urn in entities]
        return {"entities": found}

    prior = recall(urns, caller, current_policy_hash="sha256:p", now=NOW)

    assert prior[urns[0]].holds is True
    assert prior[urns[1]].holds is False
    assert "refusal" in prior[urns[1]].reason
    assert prior[urns[2]].holds is False
    assert prior[urns[2]].reason == "no receipt on this asset"


def test_recall_fails_closed_on_a_policy_change_and_on_age() -> None:
    """Yesterday's receipt under yesterday's policy vouches for nothing today."""
    urn = _dataset("aged").urn
    entities = {urn: _receipt_entity(urn, "PASS", policy_hash="sha256:old")}

    def caller(name: str, arguments: dict) -> dict:
        if name == "get_lineage":
            return _empty_lineage(name, arguments)
        return {"entities": [entities[u] for u in arguments["urns"] if u in entities]}

    repoliced = recall([urn], caller, current_policy_hash="sha256:new", now=NOW)
    aged = recall(
        [urn],
        caller,
        current_policy_hash="sha256:old",
        max_age=timedelta(minutes=30),
        now=NOW,
    )

    assert repoliced[urn].holds is False
    assert "policy hash changed" in repoliced[urn].reason
    assert aged[urn].holds is False
    assert "maximum verification age" in aged[urn].reason


def test_recall_reads_in_bounded_batches_not_one_call_per_asset() -> None:
    urns = [_dataset(f"m{index}").urn for index in range(45)]
    calls: list[int] = []

    def caller(name: str, arguments: dict) -> dict:
        calls.append(len(arguments["urns"]))
        return {"entities": []}

    prior = recall(urns, caller, now=NOW)

    assert len(prior) == 45
    assert len(calls) == 3
    assert max(calls) <= 20


def test_the_resume_flag_exists_and_is_off_by_default() -> None:
    """Amnesia is the default: nothing is skipped unless the operator opts in."""
    from sidq.cli import _parser

    assert _parser().parse_args(["audit"]).resume is False
    assert _parser().parse_args(["audit", "--resume"]).resume is True
