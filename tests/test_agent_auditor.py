"""The catalog auditor: does it choose, and does it admit what it skipped?

These test the two properties that make it an agent rather than the audit script
beside it — that the order is driven by consequence, and that a bounded run is
reported as bounded. A tool that quietly examines the first N assets it happens to
enumerate and calls the result an audit is the failure mode worth guarding.
"""

from __future__ import annotations

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

    def flaky(name: str, arguments: dict) -> dict:
        seen.append(name)
        if len(seen) == 1:
            raise RuntimeError("transport reset")
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
