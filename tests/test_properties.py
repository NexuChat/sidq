"""Let the machine look for the catalog that breaks us.

Every fixture in this repository tests a world someone imagined. That is the
limit of hand-written tests: they can only cover the failures their author
already suspects. These properties are different — Hypothesis generates
catalogs, shrinks whatever breaks to its smallest form, and reports it. The
search is not guided by what we think is dangerous.

What is asserted here are *invariants*, not answers. On a random catalog there
is no expected finding count; there are only rules the engine may never break,
whatever it is handed:

1. It terminates and does not raise.
2. Its budget is honoured exactly: examined + deferred accounts for every asset,
   and examined never exceeds the budget.
3. No asset is both `verified` and `unestablished` — "clean" and "could not be
   established" are the two answers this project exists to keep apart.
4. Only examined assets get receipts. An unexamined asset with a receipt is the
   product's cardinal sin.
5. The same catalog gives the same run, every time.
6. Every finding names a subject drawn from the catalog it was given, so the
   engine can never invent an asset.

If one of these ever fails, Hypothesis prints the minimal catalog that does it.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sidq.agent import CatalogAuditor, receipts_for
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)

# Names drawn from every script that has surprised us, plus the punctuation a
# real field path carries: dots, brackets, hashes, quotes, spaces.
_NAME_ALPHABET = st.one_of(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
        min_size=1,
        max_size=12,
    ),
    st.sampled_from(
        [
            "رقم_الهوية",
            "顧客ID",
            "счёт_номер",
            "名前",
            "geraet_id",
            "[version=2.0].[type=struct].[type=string].id",
            "[version=2.0].[type=array].[type=string].tags",
            "a.b.c",
            'quoted"name',
            "with space",
            "with#hash",
            "UPPER_CASE",
            "",
        ]
    ),
)

_PLATFORMS = st.sampled_from(
    ["snowflake", "dbt", "looker", "postgres", "s3", "kafka", "hive", "powerbi"]
)


@st.composite
def catalogs(draw: st.DrawFn) -> CatalogSnapshot:
    """A random catalog: random assets, random fields, random edges between them.

    Edges deliberately reference field names that may or may not exist on their
    endpoints, and may point at URNs that are not in the catalog at all — both
    are shapes a real ingestion produces.
    """
    count = draw(st.integers(min_value=0, max_value=8))
    entities = []
    for index in range(count):
        platform = draw(_PLATFORMS)
        fields = draw(st.lists(_NAME_ALPHABET, min_size=0, max_size=5, unique=True))
        entities.append(
            CatalogEntity(
                urn=f"urn:li:dataset:(urn:li:dataPlatform:{platform},gen.asset{index},PROD)",
                kind="dataset",
                description=draw(st.one_of(st.none(), st.text(max_size=40))),
                fields=tuple(CatalogField(name) for name in fields),
                tags=tuple(
                    draw(st.lists(st.sampled_from(["urn:li:tag:PII"]), max_size=1))
                ),
                owners=tuple(
                    draw(st.lists(st.sampled_from(["urn:li:corpuser:a"]), max_size=1))
                ),
                deprecated=draw(st.booleans()),
                schema_available=draw(st.booleans()),
            )
        )

    urns = [entity.urn for entity in entities] + [
        "urn:li:dataset:(urn:li:dataPlatform:dbt,gen.ghost,PROD)"
    ]
    edges = draw(
        st.lists(
            st.builds(
                LineageEdge,
                st.sampled_from(urns) if urns else st.just("urn:x"),
                st.one_of(st.none(), _NAME_ALPHABET),
                st.sampled_from(urns) if urns else st.just("urn:x"),
                st.one_of(st.none(), _NAME_ALPHABET),
            ),
            max_size=12,
        )
    )
    return CatalogSnapshot(tuple(entities), tuple(edges))


_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


@given(snapshot=catalogs(), budget=st.integers(min_value=0, max_value=10))
@_SETTINGS
def test_the_budget_accounts_for_every_asset(
    snapshot: CatalogSnapshot, budget: int
) -> None:
    """Nothing may vanish: every asset is examined, deferred, or vouched."""
    result = CatalogAuditor(snapshot, budget=budget).run()

    accounted = (
        set(result.examined)
        | {target.urn for target in result.deferred}
        | {urn for urn, _ in result.vouched}
    )
    assert accounted == {entity.urn for entity in snapshot.entities}
    assert len(result.examined) <= budget


@given(snapshot=catalogs())
@_SETTINGS
def test_clean_and_unestablished_never_overlap(snapshot: CatalogSnapshot) -> None:
    """ "We checked and it passed" and "we could not check" must stay distinct."""
    result = CatalogAuditor(snapshot, budget=20).run()

    assert not (set(result.verified) & set(result.unestablished))


@given(snapshot=catalogs())
@_SETTINGS
def test_only_examined_assets_can_carry_a_receipt(snapshot: CatalogSnapshot) -> None:
    """The cardinal sin: a receipt on an asset nobody looked at."""
    result = CatalogAuditor(snapshot, budget=20).run()

    receipts = receipts_for(result, commit_sha="prop")
    assert {receipt.urn for receipt in receipts} <= set(result.examined)


@given(snapshot=catalogs())
@_SETTINGS
def test_no_finding_names_an_asset_outside_the_catalog_except_orphans(
    snapshot: CatalogSnapshot,
) -> None:
    """The engine may not invent assets. Orphans are the one intended exception:
    their whole point is naming a URN the catalog does not contain."""
    result = CatalogAuditor(snapshot, budget=20).run()
    known = {entity.urn for entity in snapshot.entities}

    for finding in result.findings:
        if finding.kind == "orphan_lineage":
            continue
        subject = finding.subject.partition("#")[0]
        assert subject in known, f"invented asset in {finding.kind}: {subject}"


@given(snapshot=catalogs(), budget=st.integers(min_value=0, max_value=8))
@_SETTINGS
def test_the_same_catalog_always_produces_the_same_run(
    snapshot: CatalogSnapshot, budget: int
) -> None:
    """Determinism is the property every published verdict rests on."""
    first = CatalogAuditor(snapshot, budget=budget).run()
    second = CatalogAuditor(snapshot, budget=budget).run()

    assert first.examined == second.examined
    assert first.summary() == second.summary()
    assert [(f.kind, f.subject) for f in first.findings] == [
        (f.kind, f.subject) for f in second.findings
    ]
