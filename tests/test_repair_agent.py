"""The repair agent: does it prove its fixes, and refuse the ones that do not hold?

Proposing a fix is cheap. The properties worth pinning are the refusals — that a
repair which leaves the finding standing is rejected, that one which trades a
contradiction for a different contradiction is rejected, that individually-safe
repairs are re-proven as a set, and that nothing reaches the write path except
what the deterministic engine cleared.

The PII closure test is the one that came from live behaviour rather than from
design: the first version tagged only the column the finding named, and the engine
refused it because the newly tagged column fed an untagged consumer. A one-hop
repair moves a leak rather than closing it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.models import Evidence
from sidq.repair import (
    UNREPAIRABLE,
    apply_repairs,
    propose,
    propose_all,
    prove,
    refresh_snapshot,
    render_applied,
    render_plan,
    simulate,
    unfixed,
    verify_repairs,
)

_PII = "urn:li:tag:demo.PII_Data"


def _urn(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.{name},PROD)"


def _dataset(
    name: str,
    fields: tuple[CatalogField, ...] = (),
    owners: tuple[str, ...] = (),
) -> CatalogEntity:
    return CatalogEntity(urn=_urn(name), kind="dataset", fields=fields, owners=owners)


def _pii_chain() -> CatalogSnapshot:
    """source(tagged) -> middle(untagged) -> sink(untagged): a two-hop leak."""
    source = _dataset("source", (CatalogField("email", tags=(_PII,)),), ("u:a",))
    middle = _dataset("middle", (CatalogField("email"),), ("u:a",))
    sink = _dataset("sink", (CatalogField("email"),), ("u:a",))
    edges = (
        LineageEdge(source.urn, "email", middle.urn, "email"),
        LineageEdge(middle.urn, "email", sink.urn, "email"),
    )
    return CatalogSnapshot((source, middle, sink), edges)


def _pii_finding(target: str, column: str = "email") -> Evidence:
    return Evidence(
        "pii_leak_untagged",
        f"{target}#{column}",
        {
            "edge": {"source_urn": _urn("source"), "source_field": column},
            "source_pii_tags": [_PII],
            "target_tags": [],
            "confidence": "high",
        },
    )


def test_a_pii_repair_covers_the_whole_lineage_closure() -> None:
    """Tagging only the named column would leave the next hop leaking."""
    snapshot = _pii_chain()

    proposal = propose(_pii_finding(_urn("middle")), snapshot)

    assert proposal is not None
    assert set(proposal.targets) == {(_urn("middle"), "email"), (_urn("sink"), "email")}
    assert proposal.tool == "add_tags"


def test_the_closure_skips_columns_that_already_carry_the_marker() -> None:
    snapshot = _pii_chain()
    already = CatalogSnapshot(
        tuple(
            CatalogEntity(
                entity.urn,
                entity.kind,
                fields=tuple(
                    CatalogField(item.path, tags=(_PII,)) for item in entity.fields
                ),
                owners=entity.owners,
            )
            if entity.urn == _urn("sink")
            else entity
            for entity in snapshot.entities
        ),
        snapshot.edges,
    )

    proposal = propose(_pii_finding(_urn("middle")), already)

    assert proposal is not None
    assert set(proposal.targets) == {(_urn("middle"), "email")}


def test_the_closure_repair_is_proven_by_re_running_the_engine() -> None:
    snapshot = _pii_chain()
    proposals = propose_all([_pii_finding(_urn("middle"))], snapshot)

    plan = prove(snapshot, proposals)

    assert plan.summary()["proven"] == 1
    assert not plan.rejected
    assert plan.jointly_verified


def test_a_bounded_catalog_snapshot_cannot_prove_or_authorize_a_repair() -> None:
    complete = _pii_chain()
    proposals = propose_all([_pii_finding(_urn("middle"))], complete)
    bounded = replace(
        complete,
        entities_complete=False,
        field_lineage_resolved=frozenset({_urn("source")}),
    )

    plan = prove(bounded, proposals)

    assert not plan.proven
    assert plan.rejected
    assert not plan.jointly_verified
    assert not plan.writable
    assert "incomplete" in plan.joint_reason


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
def test_negated_pii_evidence_is_never_propagated_as_a_repair_marker(
    tag: str,
) -> None:
    finding = _pii_finding(_urn("middle"))
    finding.detail["source_pii_tags"] = [f"urn:li:tag:{tag}"]

    assert propose(finding, _pii_chain()) is None


def test_unpii_evidence_remains_a_positive_repair_marker() -> None:
    finding = _pii_finding(_urn("middle"))
    finding.detail["source_pii_tags"] = ["urn:li:tag:unpii"]

    proposal = propose(finding, _pii_chain())

    assert proposal is not None
    assert proposal.arguments["tag_urns"] == ["urn:li:tag:unpii"]


def test_a_one_hop_repair_is_refused_because_it_moves_the_leak() -> None:
    """The exact failure the live catalog produced, pinned as a regression."""
    snapshot = _pii_chain()
    proposal = propose(_pii_finding(_urn("middle")), snapshot)
    assert proposal is not None
    one_hop = type(proposal)(
        finding_kind=proposal.finding_kind,
        subject=proposal.subject,
        tool=proposal.tool,
        arguments={
            "tag_urns": [_PII],
            "entity_urns": [_urn("middle")],
            "column_paths": ["email"],
        },
        rationale="one hop only",
    )

    plan = prove(snapshot, [one_hop])

    assert not plan.proven
    assert plan.rejected[0].resolved
    assert "introduces" in plan.rejected[0].reason
    assert any("sink" in item for item in plan.rejected[0].collateral)


def test_a_marker_that_is_only_a_display_name_is_never_written() -> None:
    """Both MCP write tools take URNs; a name would be a call the server rejects."""
    finding = _pii_finding(_urn("middle"))
    finding.detail["source_pii_tags"] = ["PII_Data"]

    assert propose(finding, _pii_chain()) is None


def test_an_owner_is_proposed_only_when_every_owned_upstream_agrees() -> None:
    upstream_a = _dataset("a", (CatalogField("id"),), ("urn:li:corpuser:one",))
    upstream_b = _dataset("b", (CatalogField("id"),), ("urn:li:corpuser:one",))
    orphan = _dataset("c", (CatalogField("id"),))
    consumer = _dataset("d", (CatalogField("id"),), ("urn:li:corpuser:one",))
    snapshot = CatalogSnapshot(
        (upstream_a, upstream_b, orphan, consumer),
        (
            LineageEdge(upstream_a.urn, "id", orphan.urn, "id"),
            LineageEdge(upstream_b.urn, "id", orphan.urn, "id"),
            LineageEdge(orphan.urn, "id", consumer.urn, "id"),
        ),
    )
    finding = Evidence("unowned_consumed", orphan.urn, {"confidence": "high"})

    proposal = propose(finding, snapshot)

    assert proposal is not None
    assert proposal.arguments["owner_urns"] == ["urn:li:corpuser:one"]


def test_disagreeing_upstreams_produce_no_owner_proposal() -> None:
    """Two candidate answers means the agent would be choosing, so it declines."""
    upstream_a = _dataset("a", (CatalogField("id"),), ("urn:li:corpuser:one",))
    upstream_b = _dataset("b", (CatalogField("id"),), ("urn:li:corpuser:two",))
    orphan = _dataset("c", (CatalogField("id"),))
    snapshot = CatalogSnapshot(
        (upstream_a, upstream_b, orphan),
        (
            LineageEdge(upstream_a.urn, "id", orphan.urn, "id"),
            LineageEdge(upstream_b.urn, "id", orphan.urn, "id"),
        ),
    )

    assert propose(Evidence("unowned_consumed", orphan.urn, {}), snapshot) is None


def test_unrepairable_findings_produce_nothing_and_say_why() -> None:
    snapshot = _pii_chain()
    findings = [Evidence(kind, _urn("middle"), {}) for kind in UNREPAIRABLE]

    assert propose_all(findings, snapshot) == []
    assert all(reason for reason in UNREPAIRABLE.values())


def test_simulation_never_touches_the_real_snapshot() -> None:
    snapshot = _pii_chain()
    proposal = propose(_pii_finding(_urn("middle")), snapshot)
    assert proposal is not None

    simulate(snapshot, proposal)

    middle = next(item for item in snapshot.entities if item.urn == _urn("middle"))
    assert middle.fields[0].tags == ()


def test_nothing_is_writable_when_joint_verification_fails() -> None:
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))
    broken = type(plan)(plan.proven, plan.rejected, False, "conflict")

    calls: list[tuple[str, Any]] = []
    outcomes = apply_repairs(
        broken, lambda name, args: calls.append((name, args)), dry_run=False
    )

    assert calls == []
    assert outcomes == []


def test_joint_verification_failure_leaves_individually_proven_findings_unfixed() -> (
    None
):
    snapshot = _pii_chain()
    finding = _pii_finding(_urn("middle"))
    plan = prove(snapshot, propose_all([finding], snapshot))
    broken = type(plan)(plan.proven, plan.rejected, False, "conflict")

    assert unfixed([finding], broken) == [finding]


def test_a_dry_run_writes_nothing() -> None:
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))

    calls: list[tuple[str, Any]] = []
    outcomes = apply_repairs(plan, lambda name, args: calls.append((name, args)))

    assert calls == []
    assert outcomes and not any(item.applied for item in outcomes)


def test_one_failed_write_does_not_abandon_the_rest() -> None:
    snapshot = _pii_chain()
    orphan = _dataset("c", (CatalogField("id"),))
    upstream = _dataset("a", (CatalogField("id"),), ("urn:li:corpuser:one",))
    combined = CatalogSnapshot(
        (*snapshot.entities, orphan, upstream),
        (*snapshot.edges, LineageEdge(upstream.urn, "id", orphan.urn, "id")),
    )
    findings = [
        _pii_finding(_urn("middle")),
        Evidence("unowned_consumed", orphan.urn, {}),
    ]
    plan = prove(combined, propose_all(findings, combined))
    assert len(plan.writable) == 2

    seen: list[str] = []

    def caller(name: str, arguments: Any) -> Any:
        seen.append(name)
        if len(seen) == 1:
            raise RuntimeError("permission denied")
        return None

    outcomes = apply_repairs(plan, caller, dry_run=False)

    assert len(seen) == 2
    assert [item.applied for item in outcomes] == [False, True]


def test_successful_partial_write_can_be_verified_while_failed_write_stays_open() -> (
    None
):
    snapshot = _pii_chain()
    orphan = _dataset("c", (CatalogField("id"),))
    upstream = _dataset("a", (CatalogField("id"),), ("urn:li:corpuser:one",))
    combined = CatalogSnapshot(
        (*snapshot.entities, orphan, upstream),
        (*snapshot.edges, LineageEdge(upstream.urn, "id", orphan.urn, "id")),
    )
    plan = prove(
        combined,
        propose_all(
            [
                _pii_finding(_urn("middle")),
                Evidence("unowned_consumed", orphan.urn, {}),
            ],
            combined,
        ),
    )
    writes = 0

    def caller(name: str, arguments: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise RuntimeError("permission denied")

    outcomes = apply_repairs(plan, caller, dry_run=False)
    current = simulate(combined, outcomes[1].proposal)
    checked = verify_repairs(
        combined,
        outcomes,
        lambda proposals, timeout: current,
        timeout=0.0,
    )

    assert not checked[0].closed
    assert checked[0].status == "write_failed"
    assert checked[1].closed
    assert checked[1].status == "applied_verified"


def test_mutation_acknowledgement_stays_unverified_until_a_direct_read_proves_it() -> (
    None
):
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))
    outcomes = apply_repairs(plan, lambda name, arguments: None, dry_run=False)
    current = snapshot
    reads = 0
    now = [0.0]

    def reader(proposals, timeout):
        nonlocal current, reads
        reads += 1
        if reads == 2:
            for proposal in proposals:
                current = simulate(current, proposal)
        return current

    verified = verify_repairs(
        snapshot,
        outcomes,
        reader,
        timeout=1.0,
        monotonic=lambda: now[0],
        sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    )

    assert outcomes[0].applied
    assert not outcomes[0].verified
    assert outcomes[0].status == "applied_unverified"
    assert reads == 2
    assert verified[0].verified
    assert verified[0].closed
    assert verified[0].status == "applied_verified"


def test_verification_timeout_keeps_acknowledged_repairs_open() -> None:
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))
    outcomes = apply_repairs(plan, lambda name, arguments: None, dry_run=False)

    verified = verify_repairs(
        snapshot,
        outcomes,
        lambda proposals, timeout: snapshot,
        timeout=0.0,
    )

    assert verified[0].applied
    assert not verified[0].verified
    assert not verified[0].closed
    assert verified[0].status == "applied_unverified"
    assert "still present" in verified[0].detail
    rendered = "\n".join(render_applied(verified))
    assert "repairs verified  0 of 1" in rendered
    assert "applied_unverified" in rendered


def test_live_read_that_closes_the_target_but_adds_collateral_is_unverified() -> None:
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))
    outcomes = apply_repairs(plan, lambda name, arguments: None, dry_run=False)
    current = snapshot
    for proposal in plan.writable:
        current = simulate(current, proposal)
    current = replace(
        current,
        entities=tuple(
            replace(entity, owners=()) if entity.urn == _urn("middle") else entity
            for entity in current.entities
        ),
    )

    verified = verify_repairs(
        snapshot,
        outcomes,
        lambda proposals, timeout: current,
        timeout=0.0,
    )

    assert not verified[0].verified
    assert tuple((item.kind, item.subject) for item in verified[0].collateral) == (
        ("unowned_consumed", _urn("middle")),
    )
    assert "collateral" in verified[0].detail
    assert "unowned_consumed" in verified[0].detail
    rendered = "\n".join(render_applied(verified))
    assert f"unowned_consumed on {_urn('middle')}" in rendered


def test_incomplete_post_write_evidence_cannot_manufacture_clean_convergence() -> None:
    snapshot = _pii_chain()
    plan = prove(snapshot, propose_all([_pii_finding(_urn("middle"))], snapshot))
    outcomes = apply_repairs(plan, lambda name, arguments: None, dry_run=False)
    current = snapshot
    for proposal in plan.writable:
        current = simulate(current, proposal)
    current = replace(
        current,
        entities_complete=False,
        field_lineage_resolved=frozenset({_urn("source")}),
    )

    checked = verify_repairs(
        snapshot,
        outcomes,
        lambda proposals, timeout: current,
        timeout=0.0,
    )

    assert not checked[0].verified
    assert not checked[0].closed
    assert "incomplete" in checked[0].detail


def test_post_write_refresh_reads_target_entities_directly_without_search() -> None:
    snapshot = _pii_chain()
    proposal = propose(_pii_finding(_urn("middle")), snapshot)
    assert proposal is not None
    calls: list[str] = []

    class _DirectGraph:
        def get_dataset(self, urn: str) -> DatasetInfo:
            calls.append(urn)
            entity = next(item for item in snapshot.entities if item.urn == urn)
            return DatasetInfo(
                urn=urn,
                fields=tuple(
                    SchemaField(
                        field.path,
                        "string",
                        True,
                        field.description,
                        tags=(_PII,),
                    )
                    for field in entity.fields
                ),
                owners=entity.owners,
            )

        def search_assets(self, query: str):
            raise AssertionError("post-write verification must not search")

    refreshed = refresh_snapshot(snapshot, [proposal], _DirectGraph())

    assert calls == [_urn("middle"), _urn("sink")]
    assert not prove(
        refreshed, propose_all([_pii_finding(_urn("middle"))], refreshed)
    ).writable


def test_the_report_names_what_it_refused() -> None:
    snapshot = _pii_chain()
    proposal = propose(_pii_finding(_urn("middle")), snapshot)
    assert proposal is not None
    one_hop = type(proposal)(
        proposal.finding_kind,
        proposal.subject,
        proposal.tool,
        {
            "tag_urns": [_PII],
            "entity_urns": [_urn("middle")],
            "column_paths": ["email"],
        },
        "one hop only",
    )

    text = "\n".join(render_plan(prove(snapshot, [one_hop])))

    assert "Refused" in text
    assert "would introduce" in text


def test_unfixed_reports_what_the_plan_did_not_repair() -> None:
    snapshot = _pii_chain()
    findings = [
        _pii_finding(_urn("middle")),
        Evidence("orphan_lineage", _urn("sink"), {}),
    ]
    plan = prove(snapshot, propose_all(findings, snapshot))

    remaining = unfixed(findings, plan)

    assert [item.kind for item in remaining] == ["orphan_lineage"]
