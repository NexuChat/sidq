"""The engine, held to catalogs from domains and languages it never grew up in.

`tests/fixtures/catalog_corpus.py` builds hospitals, banks, factories, and
public registries named in Arabic, Chinese, Japanese, Russian, and German, with
the field-path conventions each platform actually imposes. Each one states what
was planted in it, so these tests assert the two halves that matter equally:
every planted contradiction is found, and nothing that was not planted is
invented.

The second half is the one that catches a tool drifting toward noise. A
verification engine that reports something on every catalog is not verifying —
it is guessing with confidence.
"""

from __future__ import annotations

import pytest

from sidq.agent import CatalogAuditor, receipts_for
from sidq.gates.self_contradiction import SelfContradictionGate
from sidq.repair import propose_all, prove
from tests.fixtures.catalog_corpus import ALL_CORPORA, Corpus


def _gate_findings(corpus: Corpus) -> dict[str, int]:
    class Scoped:
        def catalog_snapshot(self):
            return corpus.snapshot

    counts: dict[str, int] = {}
    for item in SelfContradictionGate().collect((), Scoped()):
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return counts


@pytest.mark.parametrize("build", ALL_CORPORA, ids=lambda b: b.__name__)
def test_every_planted_contradiction_is_found_and_none_are_invented(build) -> None:
    corpus = build()
    counts = _gate_findings(corpus)

    assert counts.get("lineage_field_missing", 0) == corpus.planted_field_missing, (
        f"{corpus.name}: field-missing count drifted"
    )
    assert counts.get("orphan_lineage", 0) == corpus.planted_orphans, (
        f"{corpus.name}: orphan count drifted"
    )
    assert counts.get("doc_references_missing_column", 0) == corpus.planted_doc_rot, (
        f"{corpus.name}: doc-rot count drifted"
    )


@pytest.mark.parametrize("build", ALL_CORPORA, ids=lambda b: b.__name__)
def test_the_whole_pipeline_survives_every_corpus(build) -> None:
    """Audit, receipt-building, and repair proposal must all complete."""
    corpus = build()

    result = CatalogAuditor(corpus.snapshot, budget=500).run()
    receipts = receipts_for(result, commit_sha="corpus")
    plan = prove(corpus.snapshot, propose_all(result.findings, corpus.snapshot))

    # Nothing examined may be both clean and unestablished, and nothing skipped
    # may carry a receipt.
    assert not (set(result.verified) & set(result.unestablished))
    assert {r.urn for r in receipts} <= set(result.examined)
    assert plan.summary()["proposed"] == len(plan.proven) + len(plan.rejected)


def test_a_column_named_in_arabic_or_japanese_is_not_invisible_to_doc_rot() -> None:
    """Doc rot was ASCII-only, so no non-English catalog could ever report it.

    The pattern was `[a-z][a-z0-9]*_[a-z0-9_]+`: an Arabic or Japanese column
    named in a description could not match it, so the check ran on every
    non-English catalog and found nothing — silence that read as health. Building
    a Japanese retail catalog is what exposed it.
    """
    from sidq.gates.self_contradiction import _COLUMN_REFERENCE

    assert _COLUMN_REFERENCE.findall("column 旧_顧客番号 is the legacy key") == [
        "旧_顧客番号"
    ]
    assert _COLUMN_REFERENCE.findall("العمود رقم_الهوية column") == []  # needs keyword
    assert _COLUMN_REFERENCE.findall("the column رقم_الهوية was dropped") == [
        "رقم_الهوية"
    ]
    assert _COLUMN_REFERENCE.findall("column счёт_номер removed") == ["счёт_номер"]
    # and the English behaviour is unchanged
    assert _COLUMN_REFERENCE.findall("column cust_email is gone") == ["cust_email"]


def test_a_large_corpus_stays_bounded_and_finds_nothing_to_report() -> None:
    """300 downstream assets, no contradictions: speed and silence are both correct."""
    from tests.fixtures.catalog_corpus import iot_telemetry

    corpus = iot_telemetry(devices=300)
    result = CatalogAuditor(corpus.snapshot, budget=400).run()

    assert result.covered == 301
    assert result.findings == []
