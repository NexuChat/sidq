"""The boundary: a model may choose what to test, never what is true.

`sidq claims` is the one command that lets a model participate, so the shape of
that participation is what these pin. The dangerous failure is not a model that
reads a sentence wrongly — it is a model whose reading reaches a verdict without
passing through a query. Every test below exists to make that impossible to
reintroduce quietly.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidq.claims.attest import Attestation, DocumentationAttester, datasets_from, render
from sidq.claims.models import Claim
from sidq.claims.verify import ClaimVerification
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.models import Evidence
from sidq.policy.engine import PolicyEngine

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


def _dataset(*fields: tuple[str, str | None]) -> DatasetInfo:
    return DatasetInfo(
        urn=URN,
        fields=tuple(
            SchemaField(path=name, native_type="TEXT", nullable=True, description=text)
            for name, text in fields
        ),
    )


class _Verifier:
    """Stands in for the SQL round trip, so a test can choose what came back."""

    def __init__(self, status: str = "holds", rows: int | None = 0) -> None:
        self.status = status
        self.rows = rows
        self.asked: list[Claim] = []

    def verify(self, claim: Claim, urn: str) -> ClaimVerification:
        self.asked.append(claim)
        return ClaimVerification(
            status=self.status,  # type: ignore[arg-type]
            evidence=Evidence(
                kind=f"doc_claim_{self.status}",
                subject=f"{urn}#{claim.column}",
                detail={"source_sentence": claim.source_sentence},
            ),
            violating_row_count=self.rows,
        )


class _Extractor:
    """A model stand-in: says whatever the test needs, whenever it is consulted."""

    def __init__(self, claim: Claim | None, *, raises: bool = False) -> None:
        self._claim = claim
        self._raises = raises
        self.calls: list[str] = []

    def extract(self, sentence: str, column: str, context: Any) -> Claim | None:
        del context
        self.calls.append(sentence)
        if self._raises:
            raise RuntimeError("model runtime died mid-sentence")
        return self._claim


def _model_claim(column: str = "status", confidence: float = 0.8) -> Claim:
    return Claim(
        "not_null",
        column,
        source_sentence="a sentence no rule would commit to",
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# The order of the two readers
# ---------------------------------------------------------------------------


def test_the_model_is_never_asked_about_a_sentence_the_rules_understood() -> None:
    """A model must not be able to overturn a deterministic reading, only extend it."""
    extractor = _Extractor(_model_claim())
    verifier = _Verifier()

    run = DocumentationAttester(verifier, extra=extractor).run(
        [_dataset(("customer_id", "This value must never be null."))]
    )

    assert extractor.calls == []
    assert [item.claim.origin for item in run.attestations] == ["rule"]


def test_the_model_reaches_exactly_the_sentences_the_rules_declined() -> None:
    extractor = _Extractor(_model_claim())
    run = DocumentationAttester(_Verifier(), extra=extractor).run(
        [
            _dataset(
                ("customer_id", "This value must never be null."),
                ("status", "Roughly speaking this tends to be populated."),
            )
        ]
    )

    assert extractor.calls == ["Roughly speaking this tends to be populated."]
    assert sorted(item.claim.origin for item in run.attestations) == ["model", "rule"]


def test_without_a_model_the_declined_sentences_are_reported_not_guessed() -> None:
    run = DocumentationAttester(_Verifier()).run(
        [_dataset(("status", "Roughly speaking this tends to be populated."))]
    )

    assert run.attestations == []
    assert run.declined == [("status", "Roughly speaking this tends to be populated.")]


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def test_an_untestable_model_claim_contributes_nothing_at_all() -> None:
    """The load-bearing rule. What remains of an untested claim is an opinion."""
    run = DocumentationAttester(
        _Verifier(status="unverifiable", rows=None), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    assert len(run.attestations) == 1
    assert run.admitted == []
    assert len(run.dropped) == 1
    assert run.evidence() == ()


def test_an_untestable_rule_claim_is_still_reported() -> None:
    """A deterministic reading that could not be checked is a fact about the run."""
    run = DocumentationAttester(_Verifier(status="unverifiable", rows=None)).run(
        [_dataset(("customer_id", "This value must never be null."))]
    )

    assert len(run.admitted) == 1
    assert run.dropped == []
    assert run.evidence()[0].kind == "doc_claim_unverifiable"


def test_a_model_claim_that_was_tested_counts_exactly_like_a_rule_claim() -> None:
    """Once a query has run, provenance stops mattering — the row count decides."""
    run = DocumentationAttester(
        _Verifier(status="violated", rows=3), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    assert len(run.admitted) == 1
    assert run.admitted[0].claim.origin == "model"
    assert run.evidence()[0].kind == "doc_claim_violated"


def test_a_verdict_never_rests_on_an_untested_model_reading() -> None:
    """End to end: the engine sees no evidence, so there is nothing to block on.

    Without the admissibility rule this run would have produced a
    `doc_claim_unverifiable` finding, which the shipped policy blocks on — a
    BLOCK whose entire basis was a model's reading of one English sentence.
    """
    run = DocumentationAttester(
        _Verifier(status="unverifiable", rows=None), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    verdict = PolicyEngine(None).decide(run.evidence(), commit_sha="")

    assert verdict.decision == "PASS"
    assert verdict.findings == ()


def test_an_extractor_cannot_launder_its_output_as_a_deterministic_reading() -> None:
    """Origin is stamped by the caller, so a hostile extractor cannot claim `rule`."""
    liar = _Extractor(
        Claim(
            "not_null",
            "status",
            source_sentence="a sentence no rule would commit to",
            confidence=1.0,
            origin="rule",
        )
    )

    run = DocumentationAttester(_Verifier(), extra=liar).run(
        [_dataset(("status", "a sentence no rule would commit to"))]
    )

    assert [item.claim.origin for item in run.attestations] == ["model"]


def test_an_unrecognised_origin_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="origin"):
        Claim(
            "not_null", "status", source_sentence="x", confidence=1.0, origin="oracle"
        )  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Failure modes of the optional half
# ---------------------------------------------------------------------------


def test_a_model_that_crashes_is_a_model_that_abstained() -> None:
    """Nothing downstream depends on the model having spoken, so nothing breaks."""
    run = DocumentationAttester(_Verifier(), extra=_Extractor(None, raises=True)).run(
        [_dataset(("status", "Roughly speaking this tends to be populated."))]
    )

    assert run.attestations == []
    assert len(run.declined) == 1


def test_low_confidence_proposals_are_not_tested() -> None:
    run = DocumentationAttester(
        _Verifier(), extra=_Extractor(_model_claim(confidence=0.2)), min_confidence=0.5
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    assert run.attestations == []


def test_a_dataset_the_source_refuses_does_not_lose_the_rest_of_the_run() -> None:
    class _Refusing:
        def get_dataset(self, urn: str) -> DatasetInfo | None:
            if urn.endswith("broken,PROD)"):
                raise RuntimeError("catalog refused")
            return _dataset(("customer_id", "This value must never be null."))

    found = datasets_from(
        _Refusing(),
        ["urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.broken,PROD)", URN],
    )

    assert [dataset.urn for dataset in found] == [URN]


# ---------------------------------------------------------------------------
# What the reader is told
# ---------------------------------------------------------------------------


def test_the_budget_bounds_tests_without_hiding_what_was_documented() -> None:
    """A bounded run must not read like a complete one."""
    run = DocumentationAttester(_Verifier()).run(
        [
            _dataset(
                ("a", "This value must never be null."),
                ("b", "This value must never be null."),
                ("c", "This value must never be null."),
            )
        ],
        budget=1,
    )

    assert run.summary()["documented_fields"] == 3
    assert run.summary()["claims_proposed"] == 1


def test_the_report_names_the_origin_of_every_violation() -> None:
    """A reader must be able to see which findings a model proposed."""
    run = DocumentationAttester(
        _Verifier(status="violated", rows=4), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    text = "\n".join(render(run, "WARN"))

    assert "[model]" in text
    assert "4 rows disagree" in text
    assert "WARN" in text


def test_the_report_names_the_exact_reader_that_proposed_queries() -> None:
    """Warnings from optional inference must be attributable, not just labelled model."""
    run = DocumentationAttester(
        _Verifier(status="violated", rows=4), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    text = "\n".join(
        render(
            run,
            "WARN",
            reader_identity={
                "model": "microsoft/harrier-oss-v1-270m",
                "revision": "31de22b6",
                "head_sha256": "a1b2c3d4",
                "threshold": 0.51,
            },
        )
    )

    assert "microsoft/harrier-oss-v1-270m@31de22b6" in text
    assert "head a1b2c3d4" in text
    assert "threshold 0.51" in text


def test_the_summary_counts_dropped_claims_rather_than_forgetting_them() -> None:
    """Silently discarding them would make a partial run look like a clean one."""
    run = DocumentationAttester(
        _Verifier(status="unverifiable", rows=None), extra=_Extractor(_model_claim())
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    assert run.summary()["dropped_untestable_model_claims"] == 1
    assert "dropped untested" in "\n".join(render(run))


def test_admissibility_is_a_property_of_the_attestation_not_of_the_report() -> None:
    """The report reads the rule; it does not re-implement it."""
    untested = Attestation(
        URN,
        # Constructed directly, so the origin has to be explicit — the attester
        # is what stamps it in a real run, and nothing else may.
        Claim(
            "not_null",
            "status",
            source_sentence="a sentence no rule would commit to",
            confidence=0.8,
            origin="model",
        ),
        ClaimVerification(
            status="unverifiable",
            evidence=Evidence(kind="doc_claim_unverifiable", subject=URN, detail={}),
            violating_row_count=None,
        ),
    )

    assert not untested.admissible


def test_the_reader_is_given_the_table_the_sentence_belongs_to() -> None:
    """Training saw the table name; inference must too, or it drifts off-distribution.

    Nothing fails loudly when this regresses. The reader simply abstains more
    often, on input it was never fitted for, and the only symptom is a coverage
    number nobody can explain — so the contract is asserted instead of trusted.
    """
    seen: list[Any] = []

    class _Recording:
        def extract(self, sentence: str, column: str, context: Any) -> Claim | None:
            seen.append(context)
            return None

    DocumentationAttester(_Verifier(), extra=_Recording()).run(
        [_dataset(("status", "Roughly speaking this tends to be populated."))]
    )

    assert seen and seen[0]["table_name"] == "orders"


@pytest.mark.parametrize("status", ("holds", "violated", "unverifiable"))
def test_a_model_proposed_claim_can_never_reach_a_BLOCK(status: str) -> None:
    """The safety property, stated over every outcome a query can have.

    It holds for two independent reasons, and both are load-bearing. An
    unverifiable model claim is dropped before the engine sees it, so the
    blocking `doc_claim_unverifiable` rule cannot fire on one. A violated model
    claim does reach the engine, but `doc_claim_violated` is a warning — a
    documented sentence disagreeing with the data is worth a human's attention,
    not an automatic refusal.

    So the worst a wrong reading can do is ask someone to look. If either the
    admissibility rule or that severity changes, this fails.
    """
    run = DocumentationAttester(
        _Verifier(status=status, rows=2 if status == "violated" else 0),
        extra=_Extractor(_model_claim()),
    ).run([_dataset(("status", "Roughly speaking this tends to be populated."))])

    verdict = PolicyEngine(None).decide(run.evidence(), commit_sha="")

    assert verdict.decision != "BLOCK"
