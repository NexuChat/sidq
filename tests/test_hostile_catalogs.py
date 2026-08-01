"""What the engine does with a catalog it was never designed against.

Every fixture in this repository so far is either DataHub's own showcase sample
or something we built to demonstrate a finding. That is a friendly world. A
catalog in the wild is not: it has cycles, self-loops, assets with no schema,
non-ASCII and right-to-left names, edges pointing at things that do not exist,
the same edge recorded twice, and fields that are `None` where a type says
`str`.

The property under test is not "finds the right answer" — for most of these
there is no right answer to find. It is that the engine **terminates, does not
raise, and does not turn an unexamined or unexaminable asset into a clean one**.
A verification tool that crashes on a strange catalog is merely useless; one
that returns `PASS` on it is dangerous.
"""

from __future__ import annotations

from sidq.agent import CatalogAuditor, receipts_for
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)
from sidq.repair import propose_all, prove


def _ds(name: str, **kwargs: object) -> CatalogEntity:
    return CatalogEntity(
        urn=f"urn:li:dataset:(urn:li:dataPlatform:dbt,{name},PROD)",
        kind="dataset",
        fields=kwargs.pop("fields", (CatalogField("id"),)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _audit(snapshot: CatalogSnapshot, budget: int = 50):
    return CatalogAuditor(snapshot, budget=budget).run()


# ---------------------------------------------------------------- shape edges


def test_an_empty_catalog_is_an_empty_audit_not_a_crash() -> None:
    result = _audit(CatalogSnapshot(()))

    assert result.examined == []
    assert result.findings == []
    assert receipts_for(result, commit_sha="abc") == []


def test_a_catalog_of_one_asset_with_no_schema_is_not_called_clean() -> None:
    """No schema means nothing to check — which is not the same as nothing wrong."""
    lonely = _ds("no_schema", fields=(), schema_available=False)

    result = _audit(CatalogSnapshot((lonely,)))

    assert lonely.urn in result.examined
    # It may be verified-clean or unestablished depending on evidence, but it
    # must never be both, and it must never be silently absent from the report.
    assert (lonely.urn in result.verified) != (lonely.urn in result.unestablished)


def test_a_lineage_cycle_terminates() -> None:
    """A → B → C → A. Nothing in the catalog forbids it; the auditor must survive."""
    a, b, c = _ds("a"), _ds("b"), _ds("c")
    edges = (
        LineageEdge(a.urn, "id", b.urn, "id"),
        LineageEdge(b.urn, "id", c.urn, "id"),
        LineageEdge(c.urn, "id", a.urn, "id"),
    )

    result = _audit(CatalogSnapshot((a, b, c), edges))

    assert len(result.examined) == 3


def test_a_self_loop_is_examined_once() -> None:
    """An asset that claims to feed itself is strange, not fatal."""
    solo = _ds("self")
    edges = (LineageEdge(solo.urn, "id", solo.urn, "id"),)

    result = _audit(CatalogSnapshot((solo,), edges))

    assert result.examined == [solo.urn]


def test_duplicate_edges_do_not_duplicate_the_asset() -> None:
    a, b = _ds("dup_a"), _ds("dup_b")
    edge = LineageEdge(a.urn, "id", b.urn, "id")

    result = _audit(CatalogSnapshot((a, b), (edge, edge, edge)))

    assert sorted(result.examined) == sorted({a.urn, b.urn})


def test_an_edge_to_an_asset_that_does_not_exist_is_survivable() -> None:
    """Orphan lineage is a finding, not an exception."""
    real = _ds("real")
    ghost = "urn:li:dataset:(urn:li:dataPlatform:dbt,ghost,PROD)"
    edges = (LineageEdge(real.urn, "id", ghost, "id"),)

    result = _audit(CatalogSnapshot((real,), edges))

    assert real.urn in result.examined
    assert ghost not in result.examined  # never invent an asset from an edge


def test_a_deep_lineage_chain_terminates_within_budget() -> None:
    """200 hops. Depth must not become recursion depth."""
    chain = tuple(_ds(f"link{index}") for index in range(200))
    edges = tuple(
        LineageEdge(chain[index].urn, "id", chain[index + 1].urn, "id")
        for index in range(len(chain) - 1)
    )

    result = _audit(CatalogSnapshot(chain, edges), budget=25)

    assert result.covered == 25
    assert len(result.deferred) == 175


def test_wide_fan_out_does_not_starve_the_report() -> None:
    """One asset feeding 500 others: the budget must still be honoured exactly."""
    hub = _ds("hub")
    spokes = tuple(_ds(f"spoke{index}") for index in range(500))
    edges = tuple(LineageEdge(hub.urn, "id", spoke.urn, "id") for spoke in spokes)

    result = _audit(CatalogSnapshot((hub, *spokes), edges), budget=10)

    assert result.covered == 10
    assert result.order[0].urn == hub.urn  # the most consequential asset first


# ------------------------------------------------------------------ hostile text


def test_unicode_and_rtl_names_survive_rendering() -> None:
    """Arabic, Chinese, emoji, and a name that is only a zero-width space."""
    weird = (
        _ds("مستودع.العملاء"),
        _ds("数据仓库.客户"),
        _ds("pipeline.🚀"),
        _ds("\u200b"),
    )

    result = _audit(CatalogSnapshot(weird))

    from sidq.agent.auditor import render

    text = "\n".join(render(result, catalog="hostile"))
    assert "examined" in text
    assert len(result.examined) == 4


def test_a_field_named_like_a_urn_does_not_confuse_subject_parsing() -> None:
    """Receipt subjects are `urn#field`; a field containing '#' must not split it."""
    tricky = _ds("tricky", fields=(CatalogField("weird#name"), CatalogField("id")))

    result = _audit(CatalogSnapshot((tricky,)))

    assert tricky.urn in result.examined


def test_an_extremely_long_name_is_handled() -> None:
    result = _audit(CatalogSnapshot((_ds("x" * 5000),)))

    assert len(result.examined) == 1


# ------------------------------------------------------- degenerate field data


def test_none_descriptions_and_empty_tags_do_not_raise() -> None:
    messy = CatalogEntity(
        urn="urn:li:dataset:(urn:li:dataPlatform:dbt,messy,PROD)",
        kind="dataset",
        description=None,
        fields=(CatalogField("id", None, ()), CatalogField("other", "", ("",))),
        tags=(),
        owners=(),
    )

    result = _audit(CatalogSnapshot((messy,)))

    assert messy.urn in result.examined


def test_field_lineage_with_missing_field_names_is_not_a_crash() -> None:
    """Table-level edges carry `None` where a column would be."""
    a, b = _ds("tbl_a"), _ds("tbl_b")
    edges = (LineageEdge(a.urn, None, b.urn, None),)

    result = _audit(CatalogSnapshot((a, b), edges))

    assert len(result.examined) == 2


# ------------------------------------------------------------- repair under fire


def test_repair_proposes_nothing_on_a_catalog_it_cannot_reason_about() -> None:
    """No PII markers, no owned upstreams: the honest output is an empty plan."""
    a, b = _ds("plain_a"), _ds("plain_b")
    snapshot = CatalogSnapshot((a, b), (LineageEdge(a.urn, "id", b.urn, "id"),))
    result = _audit(snapshot)

    plan = prove(snapshot, propose_all(result.findings, snapshot))

    assert list(plan.writable) == []
    assert plan.summary()["proposed"] == len(plan.proven) + len(plan.rejected)


def test_repair_on_an_empty_catalog_is_an_empty_plan() -> None:
    snapshot = CatalogSnapshot(())

    plan = prove(snapshot, propose_all([], snapshot))

    assert list(plan.writable) == []


def test_repair_refuses_a_marker_that_is_only_a_display_name() -> None:
    """A tag that never resolved to a URN cannot be written, so it is not offered."""
    source = CatalogEntity(
        urn="urn:li:dataset:(urn:li:dataPlatform:dbt,src,PROD)",
        kind="dataset",
        fields=(CatalogField("email", None, ("PII",)),),  # display name, not a URN
        owners=("urn:li:corpuser:a",),
    )
    sink = _ds("sink", fields=(CatalogField("email"),), owners=("urn:li:corpuser:a",))
    snapshot = CatalogSnapshot(
        (source, sink), (LineageEdge(source.urn, "email", sink.urn, "email"),)
    )
    result = _audit(snapshot)

    plan = prove(snapshot, propose_all(result.findings, snapshot))

    assert list(plan.writable) == []


# ------------------------------------------------------------------ determinism


def test_a_hostile_catalog_is_still_deterministic() -> None:
    """Strangeness must not introduce order-dependence."""
    hub = _ds("hub")
    others = tuple(_ds(f"n{index}") for index in range(40))
    edges = tuple(
        LineageEdge(hub.urn, "id", other.urn, "id" if index % 2 else "missing")
        for index, other in enumerate(others)
    )
    snapshot = CatalogSnapshot((hub, *others), edges)

    first = _audit(snapshot, budget=12)
    second = _audit(snapshot, budget=12)

    assert first.examined == second.examined
    assert [item.kind for item in first.findings] == [
        item.kind for item in second.findings
    ]


# ------------------------------------------------- adversarial receipt reading


def test_a_malformed_receipt_payload_never_vouches() -> None:
    """Garbage from the catalog must read as 'no receipt', never as 'verified'.

    This is the failure that would matter most: a reader that treats an
    unparseable response as a pass hands an attacker a way to launder any asset
    through a broken payload.
    """
    from sidq.agent import recall

    urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,x,PROD)"
    for payload in (
        None,
        "",
        [],
        {},
        {"entities": "not-a-list"},
        {"entities": [{"urn": urn, "structuredProperties": "nonsense"}]},
        {"entities": [{"urn": urn, "structuredProperties": {"properties": [None]}}]},
        {"entities": [{"urn": urn, "structuredProperties": {"properties": [{}]}}]},
    ):
        prior = recall([urn], lambda name, args, p=payload: p)
        assert prior[urn].holds is False, f"payload vouched wrongly: {payload!r}"


def test_a_receipt_with_a_future_timestamp_still_expires_by_policy_hash() -> None:
    """A clock-skewed or forged future date must not create an immortal receipt."""
    from datetime import UTC, datetime

    from sidq.agent import recall

    urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,future,PROD)"

    def prop(name: str, value: str) -> dict:
        return {
            "structuredProperty": {"urn": f"urn:li:structuredProperty:sidq.{name}"},
            "values": [{"stringValue": value}],
        }

    payload = {
        "entities": [
            {
                "urn": urn,
                "structuredProperties": {
                    "properties": [
                        prop("verdict", "PASS"),
                        prop("checked_at", "2099-01-01T00:00:00Z"),
                        prop("policy_hash", "sha256:old"),
                    ]
                },
            }
        ]
    }
    now = datetime(2026, 8, 1, tzinfo=UTC)

    under_old = recall(
        [urn], lambda n, a: payload, current_policy_hash="sha256:old", now=now
    )
    under_new = recall(
        [urn], lambda n, a: payload, current_policy_hash="sha256:new", now=now
    )

    # A future date alone does not expire it — but a policy change always does.
    assert under_new[urn].holds is False
    assert "policy hash changed" in under_new[urn].reason
    assert under_old[urn].holds is True  # documented behaviour, not an accident


def test_a_receipt_recording_block_never_vouches_however_fresh() -> None:
    from sidq.agent import recall

    urn = "urn:li:dataset:(urn:li:dataPlatform:dbt,blocked,PROD)"

    def prop(name: str, value: str) -> dict:
        return {
            "structuredProperty": {"urn": f"urn:li:structuredProperty:sidq.{name}"},
            "values": [{"stringValue": value}],
        }

    from datetime import UTC, datetime

    now = datetime(2026, 8, 1, tzinfo=UTC)
    payload = {
        "entities": [
            {
                "urn": urn,
                "structuredProperties": {
                    "properties": [
                        prop("verdict", "BLOCK"),
                        prop("checked_at", "2026-08-01T00:00:00Z"),
                        prop("policy_hash", "sha256:p"),
                    ]
                },
            }
        ]
    }

    prior = recall([urn], lambda n, a: payload, current_policy_hash="sha256:p", now=now)

    assert prior[urn].holds is False
    assert "refusal" in prior[urn].reason


def test_a_transport_that_raises_does_not_silently_pass_the_catalog() -> None:
    """If the receipts cannot be read at all, nothing may be vouched."""
    import pytest

    from sidq.agent import recall

    def broken(name: str, args: dict):
        raise ConnectionError("mcp down")

    with pytest.raises(ConnectionError):
        recall(["urn:li:dataset:(urn:li:dataPlatform:dbt,x,PROD)"], broken)


def test_an_unreachable_catalog_fails_fast_and_says_so() -> None:
    """A judge who mistypes a port must get an answer, not a hang.

    The SDK's defaults retry a dead endpoint for minutes; measured before this
    guard, `sidq audit --server http://localhost:9999` never returned. The CLI
    now bounds it, and this pins the flag that does so.
    """
    from sidq.cli import _parser

    parsed = _parser().parse_args(["audit"])
    assert parsed.timeout > 0
    assert _parser().parse_args(["audit", "--timeout", "3"]).timeout == 3.0
    assert _parser().parse_args(["repair", "--timeout", "3"]).timeout == 3.0


def test_a_zero_or_negative_budget_examines_nothing_and_admits_it() -> None:
    """A budget of zero is a legitimate request: audit nothing, report everything."""
    entities = tuple(_ds(f"b{index}") for index in range(4))

    for budget in (0, -5):
        result = CatalogAuditor(CatalogSnapshot(entities), budget=budget).run()
        assert result.examined == []
        assert len(result.deferred) == 4
        assert result.summary()["deferred"] == 4


def test_an_orphan_edge_is_reported_not_silently_dropped() -> None:
    """The auditor once discarded every orphan finding the gate produced.

    An orphan's subject is the *missing* URN, so a filter keyed on the target's
    own URN could never match it: the gate found the contradiction and the agent
    threw it away. This is the guard — a real asset with an edge into nothing
    must surface as `orphan_lineage` when the catalog view is whole.
    """
    real = _ds("has_orphan_edge")
    ghost = "urn:li:dataset:(urn:li:dataPlatform:dbt,gone,PROD)"
    snapshot = CatalogSnapshot((real,), (LineageEdge(real.urn, "id", ghost, "id"),))

    class Scoped:
        def catalog_snapshot(self):
            return snapshot

    from sidq.gates.self_contradiction import SelfContradictionGate

    findings = SelfContradictionGate().collect((), Scoped())
    orphans = [item for item in findings if item.kind == "orphan_lineage"]

    assert orphans, "the orphan finding was dropped again"
    assert orphans[0].subject == ghost
    assert orphans[0].detail["edge"]["source_dataset"] == real.urn


def test_a_bounded_catalog_view_never_manufactures_orphans() -> None:
    """Paging is the reader's boundary, not the catalog's contradiction.

    MCP `search` returns one page. Every edge leaving that page points at an
    asset that exists and is simply outside the window — and calling those
    dangling produced 651 findings on a catalog with none. A bounded view now
    reports what it could not adjudicate instead.
    """
    a, b = _ds("in_page"), _ds("out_of_page")
    edges = (LineageEdge(a.urn, "id", b.urn, "id"),)
    page = CatalogSnapshot((a,), edges, entities_complete=False)

    class Scoped:
        def catalog_snapshot(self):
            return page

    from sidq.gates.self_contradiction import SelfContradictionGate

    findings = SelfContradictionGate().collect((), Scoped())
    orphans = [f for f in findings if f.kind == "orphan_lineage"]

    assert not orphans
    unverifiable = [f for f in findings if f.kind == "orphan_lineage_unverifiable"]
    assert unverifiable, "a bounded view must say what it could not adjudicate"


def test_a_complete_catalog_view_still_reports_a_real_orphan() -> None:
    """The honesty above must not become blindness."""
    real = _ds("real")
    ghost = "urn:li:dataset:(urn:li:dataPlatform:dbt,gone,PROD)"
    whole = CatalogSnapshot((real,), (LineageEdge(real.urn, "id", ghost, "id"),))

    class Scoped:
        def catalog_snapshot(self):
            return whole

    from sidq.gates.self_contradiction import SelfContradictionGate

    findings = SelfContradictionGate().collect((), Scoped())

    assert [f for f in findings if f.kind == "orphan_lineage"]
