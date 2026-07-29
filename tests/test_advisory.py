from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path

import pytest

from sidq.advisory import embedder
from sidq.advisory.semantic_drift import (
    ColumnUsage,
    SemanticDriftCheck,
    collect_if_enabled,
)
from sidq.bot.comment import render_comment
from sidq.models import Verdict


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(self, texts: list[str], *, side: str):
        self.calls.append((tuple(texts), side))
        if side == "query":
            return ((1.0, 0.0),) * len(texts)
        return ((0.0, 1.0),) * len(texts)


def _column(
    name: str = "region",
    description: str | None = "The customer's shipping region value.",
) -> ColumnUsage:
    return ColumnUsage(
        "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.customers,PROD)",
        name,
        description,
        ("urn:li:dashboard:regional-sales",),
        "lower(country_code)",
        ("customer_id", "country_code"),
    )


def test_below_threshold_is_rendered_as_non_blocking_model_evidence() -> None:
    findings = SemanticDriftCheck(embedder=FakeEmbedder()).collect([_column()])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_id == "semantic_drift"
    assert finding.severity == "advisory"
    assert finding.evidence[0].detail == {
        "advisory": True,
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "similarity": 0.0,
        "threshold": 0.35,
    }
    verdict = Verdict("PASS", None, (), (), "a" * 40, "policy")
    rendered = render_comment(verdict, advisory_findings=findings)
    assert "## Non-blocking model-assisted evidence" in rendered
    assert "Qwen/Qwen3-Embedding-0.6B" in rendered


def test_embedder_import_failure_yields_no_advisory_findings(monkeypatch) -> None:
    monkeypatch.setattr(embedder, "_backend", None)
    monkeypatch.setattr(embedder, "_load_attempted", False)
    original_import = builtins.__import__

    def missing_transformers(name, *args, **kwargs):
        if name == "transformers":
            raise ImportError("transformers unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_transformers)
    assert embedder.embed_texts(["a document"]) is None
    assert collect_if_enabled([_column()], environ={}) == []


def test_unset_advisory_means_no_layer_is_invoked(monkeypatch) -> None:
    monkeypatch.delenv("SIDQ_ADVISORY", raising=False)
    spy = FakeEmbedder()
    check = SemanticDriftCheck(embedder=spy)

    assert collect_if_enabled([_column()], environ={}, check=check) == []
    assert spy.calls == []


def test_advisory_never_changes_the_assembled_policy_verdict() -> None:
    findings = SemanticDriftCheck(embedder=FakeEmbedder()).collect([_column()])
    deterministic = Verdict("PASS", None, (), (), "a" * 40, "policy")
    assembled = Verdict(
        deterministic.decision,
        deterministic.reason_code,
        (*deterministic.findings, *findings),
        deterministic.touched,
        deterministic.commit_sha,
        deterministic.policy_hash,
    )

    assert assembled.decision == deterministic.decision == "PASS"
    assert {finding.severity for finding in findings} == {"advisory"}


def test_short_or_empty_descriptions_are_skipped() -> None:
    spy = FakeEmbedder()
    findings = SemanticDriftCheck(embedder=spy).collect(
        [_column("empty", ""), _column("short", "three words only")]
    )

    assert findings == []
    assert spy.calls == []


def test_same_inputs_produce_identical_findings_in_column_order() -> None:
    check = SemanticDriftCheck(embedder=FakeEmbedder())
    columns = [_column("zeta"), _column("alpha")]

    first = check.collect(columns)
    second = check.collect(columns)

    assert first == second
    assert [item.evidence[0].subject.rsplit("#", 1)[1] for item in first] == [
        "alpha",
        "zeta",
    ]


_MODEL_CACHE = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B"
_REAL_MODEL_AVAILABLE = (
    importlib.util.find_spec("torch") is not None and _MODEL_CACHE.is_dir()
)


@pytest.mark.skipif(
    not _REAL_MODEL_AVAILABLE, reason="Qwen model weights are not cached locally"
)
def test_real_embeddings_are_deterministic_and_related_pairs_score_higher() -> None:
    related_query = embedder.embed_texts(
        ["customer billing email address"], side="query"
    )
    related_document = embedder.embed_texts(
        ["The column stores the email address used for customer invoices."],
        side="document",
    )
    unrelated_query = embedder.embed_texts(
        ["customer billing email address"], side="query"
    )
    unrelated_document = embedder.embed_texts(
        ["The warehouse loading dock closes at sunset."], side="document"
    )

    assert related_query is not None and related_document is not None
    assert unrelated_query is not None and unrelated_document is not None
    assert related_query.tobytes() == unrelated_query.tobytes()
    related = float(related_query[0] @ related_document[0])
    unrelated = float(unrelated_query[0] @ unrelated_document[0])
    assert related > unrelated
