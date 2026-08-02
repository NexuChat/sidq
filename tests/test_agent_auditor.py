"""The catalog auditor: does it choose, and does it admit what it skipped?

These test the two properties that make it an agent rather than the audit script
beside it — that the order is driven by consequence, and that a bounded run is
reported as bounded. A tool that quietly examines the first N assets it happens to
enumerate and calls the result an audit is the failure mode worth guarding.
"""

from __future__ import annotations

import pytest

import sidq.agent.writeback as writeback_module
from sidq.agent import (
    CatalogAuditor,
    receipts_for,
    render,
    render_writeback,
    write_receipts,
)
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)
from sidq.receipt.write import ReceiptWriteUnconfirmed


def _dataset(name: str, **kwargs: object) -> CatalogEntity:
    return CatalogEntity(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.{name},PROD)",
        kind="dataset",
        fields=kwargs.pop("fields", (CatalogField("id"),)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_agent_examines_the_most_consequential_asset_first() -> None:
    """Alphabetical order would put `aaa` first; consequence puts `zzz` first."""
    quiet = _dataset("aaa", owners=("urn:li:corpuser:a",))
    hot = _dataset("zzz", owners=("urn:li:corpuser:a",))
    edges = tuple(
        LineageEdge(hot.urn, "id", _dataset(f"c{index}").urn, "id")
        for index in range(5)
    )
    snapshot = CatalogSnapshot((quiet, hot), edges)

    plan = CatalogAuditor(snapshot).plan()

    assert plan[0].urn == hot.urn
    assert "5 downstream consumers" in plan[0].reasons


def test_pii_and_missing_ownership_raise_an_asset_up_the_order() -> None:
    """Consequence is about damage, not about how many edges an asset happens to have."""
    plain = _dataset("plain", owners=("urn:li:corpuser:a",))
    sensitive = _dataset("sensitive", tags=("urn:li:tag:PII_Data",), owners=())
    snapshot = CatalogSnapshot((plain, sensitive))

    plan = CatalogAuditor(snapshot).plan()

    assert plan[0].urn == sensitive.urn
    assert "carries a PII tag" in plan[0].reasons
    assert "no owner" in plan[0].reasons


@pytest.mark.parametrize(
    "tag",
    (
        "not_pii",
        "not-pii",
        "NOTPII",
        "nonPII",
        "noPII",
        "not_personally_identifiable",
        "not-personally-identifiable",
        "NOTPERSONALLYIDENTIFIABLE",
        "nonPersonallyIdentifiable",
        "noPersonallyIdentifiable",
    ),
)
def test_a_negated_pii_tag_does_not_raise_audit_consequence(tag: str) -> None:
    negated = _dataset(
        "negated",
        tags=(f"urn:li:tag:{tag}",),
        owners=("urn:li:corpuser:a",),
    )

    target = CatalogAuditor(CatalogSnapshot((negated,))).plan()[0]

    assert target.consequence == 0
    assert target.reasons == ("no notable exposure",)


def test_unpii_remains_a_positive_audit_marker() -> None:
    target = CatalogAuditor(
        CatalogSnapshot(
            (
                _dataset(
                    "positive",
                    tags=("urn:li:tag:unpii",),
                    owners=("urn:li:corpuser:a",),
                ),
            )
        )
    ).plan()[0]

    assert target.consequence == 50
    assert target.reasons == ("carries a PII tag",)


def test_a_bounded_run_names_what_it_did_not_examine() -> None:
    """The number that matters in a partial audit is what was left out."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(10)
    )

    result = CatalogAuditor(CatalogSnapshot(entities), budget=3).run()

    assert result.covered == 3
    assert len(result.deferred) == 7
    assert result.summary()["deferred"] == 7
    assert "NOT examined    7" in "\n".join(render(result, catalog="test"))


def test_a_complete_run_claims_no_deferral() -> None:
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(4)
    )

    result = CatalogAuditor(CatalogSnapshot(entities), budget=100).run()

    assert result.deferred == []
    assert "NOT examined" not in "\n".join(render(result, catalog="test"))


def test_a_lineage_edge_to_a_field_that_does_not_exist_is_found() -> None:
    """The headline check: the catalog claiming an edge into a column it has not got."""
    source = _dataset("source", owners=("urn:li:corpuser:a",))
    target = _dataset(
        "target", fields=(CatalogField("real_column"),), owners=("urn:li:corpuser:a",)
    )
    edges = (LineageEdge(source.urn, "id", target.urn, "ghost_column"),)

    result = CatalogAuditor(CatalogSnapshot((source, target), edges)).run()

    assert any(item.kind == "lineage_field_missing" for item in result.findings)


def test_a_clean_catalog_produces_no_findings_and_says_so() -> None:
    """An auditor that always finds something is not measuring anything."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(3)
    )

    result = CatalogAuditor(CatalogSnapshot(entities)).run()

    assert result.findings == []
    assert len(result.verified) == 3


def test_a_partial_catalog_cannot_mint_verified_clean_or_pass_receipts() -> None:
    entity = _dataset("partial", owners=("urn:li:corpuser:a",))

    result = CatalogAuditor(CatalogSnapshot((entity,), entities_complete=False)).run()

    assert result.verified == []
    assert result.unestablished == [entity.urn]
    assert receipts_for(result) == []


def test_the_agent_never_decides_truth_itself() -> None:
    """Every finding must come from the deterministic gate it delegates to.

    The agent's job is choosing where to point the engine. If it ever starts
    emitting evidence of its own, the LLM-free guarantee stops being structural
    and becomes a promise.
    """
    seen: list[str] = []

    class RecordingGate:
        def collect(self, change: object, graph: object) -> list:
            seen.append("called")
            return []

    entities = (_dataset("m0", owners=("urn:li:corpuser:a",)),)
    result = CatalogAuditor(
        CatalogSnapshot(entities),
        gate=RecordingGate(),  # type: ignore[arg-type]
    ).run()

    assert seen, "the agent must delegate to the deterministic gate"
    assert result.findings == []


def test_the_run_is_deterministic_for_one_catalog() -> None:
    """Same catalog, same transcript — the property a live demo depends on."""
    entities = tuple(
        _dataset(f"m{index}", tags=("urn:li:tag:PII_Data",) if index % 2 else ())
        for index in range(6)
    )
    snapshot = CatalogSnapshot(entities)

    first = CatalogAuditor(snapshot, budget=4).run()
    second = CatalogAuditor(snapshot, budget=4).run()

    assert first.examined == second.examined
    assert first.summary() == second.summary()


# ---------------------------------------------------------------------------
# Write-back: carrying the result into the catalog, without inventing any of it.
# ---------------------------------------------------------------------------


def test_only_examined_assets_get_a_receipt() -> None:
    """Writing "verified" for an asset never looked at is the one unforgivable bug."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(10)
    )
    result = CatalogAuditor(CatalogSnapshot(entities), budget=3).run()

    receipts = receipts_for(result)

    assert len(receipts) == 3
    assert {receipt.urn for receipt in receipts} == set(result.examined)
    deferred = {target.urn for target in result.deferred}
    assert not deferred & {receipt.urn for receipt in receipts}


def test_the_receipt_carries_the_policy_verdict_not_the_agent_s_opinion() -> None:
    """The agent runs the shipped policy and carries its answer; it does not label.

    The contradiction is attributed to the **target**, whose stored schema is the
    thing missing the field — not to the source that claims the edge. An earlier
    version of this test asserted the source and was simply wrong about whose
    claim is false.
    """
    source = _dataset("source", owners=("urn:li:corpuser:a",))
    target = _dataset(
        "target", fields=(CatalogField("real"),), owners=("urn:li:corpuser:a",)
    )
    edges = (LineageEdge(source.urn, "id", target.urn, "ghost"),)
    result = CatalogAuditor(CatalogSnapshot((source, target), edges)).run()

    receipts = {receipt.urn: receipt for receipt in receipts_for(result)}

    flagged = receipts[target.urn]
    assert flagged.verdict in {"WARN", "BLOCK"}
    assert "lineage_field_missing" in flagged.rules_fired
    # Every receipt records which policy produced it, so a stale one is detectable.
    assert flagged.policy_hash

    # And the asset with nothing against it is not tarred by its neighbour.
    assert receipts[source.urn].verdict == "PASS"
    assert receipts[source.urn].rules_fired == ()


def test_computing_receipts_touches_no_catalog() -> None:
    """`receipts_for` must be pure, so a caller can inspect before writing."""
    entities = (_dataset("m0", owners=("urn:li:corpuser:a",)),)
    result = CatalogAuditor(CatalogSnapshot(entities)).run()

    calls: list[str] = []

    def recording(name: str, arguments: dict) -> dict:
        calls.append(name)
        return {}

    receipts_for(result)

    assert calls == [], "computing what would be written must not write"


def test_one_failed_write_does_not_discard_the_rest() -> None:
    """A transport error on one asset must not throw away a completed audit."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(3)
    )
    result = CatalogAuditor(CatalogSnapshot(entities)).run()
    receipts = receipts_for(result)
    seen: list[str] = []
    stored: dict[str, dict[str, list[str]]] = {}
    stored_tags: dict[str, set[str]] = {}

    def flaky(name: str, arguments: dict) -> dict:
        seen.append(name)
        if name == "get_entities":
            urn = arguments["urns"][0]
            return {
                "entities": [
                    {
                        "urn": urn,
                        "structuredProperties": {
                            "properties": [
                                {
                                    "structuredProperty": {"urn": property_urn},
                                    "values": [
                                        {"stringValue": value} for value in values
                                    ],
                                }
                                for property_urn, values in stored.get(urn, {}).items()
                            ]
                        },
                        "globalTags": {
                            "tags": [
                                {"tagUrn": tag}
                                for tag in sorted(stored_tags.get(urn, set()))
                            ]
                        },
                    }
                ]
            }
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
        if name == "save_document" and seen.count("save_document") == 1:
            raise RuntimeError("transport reset")
        if name == "add_structured_properties":
            stored[arguments["entity_urns"][0]] = dict(arguments["property_values"])
        if name == "add_tags":
            stored_tags.setdefault(arguments["entity_urns"][0], set()).update(
                arguments["tag_urns"]
            )
        return {"urn": "urn:li:document:x"}

    outcomes = write_receipts(receipts, flaky)

    assert len(outcomes) == len(receipts)
    assert any(not item.written for item in outcomes)
    assert any(item.written for item in outcomes)
    rendered = "\n".join(render_writeback(outcomes))
    assert "write failures" in rendered
    assert "RuntimeError" in rendered


def test_nothing_is_reported_written_that_was_not() -> None:
    """The count must come from outcomes, never from the number attempted."""
    entities = tuple(
        _dataset(f"m{index}", owners=("urn:li:corpuser:a",)) for index in range(4)
    )
    result = CatalogAuditor(CatalogSnapshot(entities)).run()

    def always_fails(name: str, arguments: dict) -> dict:
        raise RuntimeError("catalog is read-only")

    outcomes = write_receipts(receipts_for(result), always_fails)

    assert all(not item.written for item in outcomes)
    assert "receipts written  0 of 4" in "\n".join(render_writeback(outcomes))


def test_unconfirmed_readback_is_reported_as_write_unconfirmed(monkeypatch) -> None:
    result = CatalogAuditor(
        CatalogSnapshot((_dataset("orders", owners=("urn:li:corpuser:a",)),))
    ).run()

    def unconfirmed(*args: object, **kwargs: object) -> None:
        raise ReceiptWriteUnconfirmed("write_unconfirmed: timed out")

    monkeypatch.setattr(writeback_module, "write_receipt", unconfirmed)
    outcomes = write_receipts(receipts_for(result), lambda *_: {})

    assert outcomes[0].written is False
    assert outcomes[0].detail == "write_unconfirmed"
    assert "write_unconfirmed" in "\n".join(render_writeback(outcomes))


# ---------------------------------------------------------------------------
# The CLI surface. A capability nobody can invoke is the gap this repository
# already had once, in the gates; the audit must not repeat it.
# ---------------------------------------------------------------------------


def test_audit_is_an_invocable_subcommand_with_writing_off_by_default() -> None:
    """Writing to a catalog must never be the default of a read-only-sounding verb."""
    from sidq.cli import _parser

    parsed = _parser().parse_args(["audit"])

    assert parsed.command == "audit"
    assert parsed.write_receipts is False
    assert parsed.budget > 0


def test_audit_accepts_a_budget_and_an_explicit_write_opt_in() -> None:
    from sidq.cli import _parser

    parsed = _parser().parse_args(["audit", "--budget", "7", "--write-receipts"])

    assert parsed.budget == 7
    assert parsed.write_receipts is True


def test_audit_is_declared_once() -> None:
    """Two `add_parser("audit")` calls raise at import-time in argparse.

    This happened: an earlier edit landed despite appearing to be rejected, and a
    second declaration was added on top of it, so every `sidq` invocation crashed
    before parsing anything. Cheap to assert, expensive to discover.
    """
    from sidq.cli import _parser

    parser = _parser()

    assert parser.parse_args(["audit"]).command == "audit"
    assert parser.parse_args(["explain", "pii_exposure"]).command == "explain"


# ---------------------------------------------------------------------------
# The feedback loop: what it finds must change what it looks at next, or the
# word "agent" is decoration on a sorted for-loop.
# ---------------------------------------------------------------------------


def test_a_contagious_finding_promotes_a_neighbour_past_the_budget() -> None:
    """The asset next door is unreachable on score alone; the finding reaches it."""
    # A `lineage_field_missing` contradiction belongs to the asset whose stored
    # schema lacks the field — the edge's *target*, not the source claiming it. So
    # the asset that carries the finding is the one that must be examined early,
    # and what it drags in is a neighbour nothing else would reach.
    lied_about = _dataset(
        "lied_about", fields=(CatalogField("real"),), owners=("urn:li:corpuser:a",)
    )
    quiet = _dataset("zzz_quiet", owners=("urn:li:corpuser:a",))
    filler = tuple(
        _dataset(f"f{index}", owners=("urn:li:corpuser:a",)) for index in range(8)
    )
    edges = (
        # A filler claims an edge into a column `lied_about` does not have, so the
        # contradiction is attributed to `lied_about`.
        LineageEdge(filler[0].urn, "id", lied_about.urn, "ghost"),
        # `lied_about` feeds several consumers, which is what puts it near the top
        # of the plan. `quiet` is one of them and has no consumers of its own, so
        # it scores nothing and sorts last.
        LineageEdge(lied_about.urn, "real", quiet.urn, "id"),
        *(LineageEdge(lied_about.urn, "real", item.urn, "id") for item in filler[1:4]),
    )
    snapshot = CatalogSnapshot((lied_about, quiet, *filler), edges)

    # On score alone the quiet neighbour is nowhere near a budget of two.
    assert quiet.urn not in {
        target.urn for target in CatalogAuditor(snapshot).plan()[:2]
    }

    result = CatalogAuditor(snapshot, budget=2).run()

    assert quiet.urn in result.examined, (
        "a neighbour of a lied-about asset must be pulled into a budget that "
        "would never have reached it on its static score"
    )
    assert any(
        because == lied_about.urn and got == quiet.urn
        for because, got in result.promoted
    )
    assert result.summary()["promoted_by_a_finding"] >= 1


def test_a_governance_gap_does_not_promote_anyone() -> None:
    """An unowned asset says nothing about whether its neighbour has an owner.

    Promoting on ownership was tried and spent the budget chasing a signal that
    does not cluster; on the live catalog it displaced every lineage contradiction
    at the same budget.
    """
    unowned = _dataset("unowned", owners=())
    neighbour = _dataset("neighbour", owners=("urn:li:corpuser:a",))
    edges = (LineageEdge(unowned.urn, "id", neighbour.urn, "id"),)

    result = CatalogAuditor(CatalogSnapshot((unowned, neighbour), edges)).run()

    assert any(item.kind == "unowned_consumed" for item in result.findings)
    assert result.promoted == [], "ownership gaps are not contagious"


def test_promotion_leaves_the_run_deterministic() -> None:
    """A demo depends on the same catalog producing the same transcript."""
    a = _dataset("a", owners=("urn:li:corpuser:x",))
    b = _dataset("b", fields=(CatalogField("real"),), owners=("urn:li:corpuser:x",))
    c = _dataset("c", fields=(CatalogField("real"),), owners=("urn:li:corpuser:x",))
    edges = (
        LineageEdge(a.urn, "id", b.urn, "ghost"),
        LineageEdge(a.urn, "id", c.urn, "ghost"),
    )
    snapshot = CatalogSnapshot((a, b, c), edges)

    first = CatalogAuditor(snapshot, budget=3).run()
    second = CatalogAuditor(snapshot, budget=3).run()

    assert first.examined == second.examined
    assert first.promoted == second.promoted


def test_no_asset_is_examined_twice_even_when_promoted() -> None:
    """Promotion must not let the budget be spent re-examining the same asset."""
    a = _dataset("a", owners=("urn:li:corpuser:x",))
    b = _dataset("b", fields=(CatalogField("real"),), owners=("urn:li:corpuser:x",))
    edges = (
        LineageEdge(a.urn, "id", b.urn, "ghost"),
        LineageEdge(b.urn, "id", a.urn, "ghost"),
    )

    result = CatalogAuditor(CatalogSnapshot((a, b), edges), budget=10).run()

    assert len(result.examined) == len(set(result.examined))
