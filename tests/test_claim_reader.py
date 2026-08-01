"""The embedding reader may propose work, but it must stay conservative about it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from sidq.claims import reader as reader_module
from sidq.claims.reader import EmbeddingClaimReader


class _FakeEmbedder:
    """Returns a fixed vector so each test controls the linear head directly."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.texts: list[str] = []

    def encode(
        self,
        sentences: list[str],
        *,
        show_progress_bar: bool,
        normalize_embeddings: bool,
    ) -> np.ndarray[Any, Any]:
        assert show_progress_bar is False
        assert normalize_embeddings is True
        self.texts.extend(sentences)
        return np.asarray([self._vector])


def _head(
    path: Path,
    *,
    intercept: list[float],
    classes: list[int],
    threshold: float,
) -> Path:
    np.savez(
        path,
        coef=np.zeros((len(classes), 1)),
        intercept=np.asarray(intercept),
        classes=np.asarray(classes),
        threshold=np.asarray([threshold]),
    )
    return path


def _reader(
    path: Path, vector: list[float] | None = None
) -> tuple[EmbeddingClaimReader, _FakeEmbedder]:
    embedder = _FakeEmbedder(vector or [1.0])
    return EmbeddingClaimReader(path, embedder=embedder), embedder


def test_reader_abstains_below_the_saved_threshold(tmp_path: Path) -> None:
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 0.2, 0.0],
        classes=[0, 1, 2],
        threshold=0.9,
    )
    reader, _ = _reader(path)

    assert reader.extract("This identifies a row.", "id", {}) is None


def test_reader_abstains_for_a_confident_non_proposable_label(tmp_path: Path) -> None:
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 0.0, 0.0, 8.0],
        classes=[0, 1, 2, 3],
        threshold=0.5,
    )
    reader, _ = _reader(path)

    assert reader.extract("Values are active or inactive.", "status", {}) is None


def test_reader_returns_the_proposable_argmax_above_threshold(tmp_path: Path) -> None:
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 0.0, 5.0],
        classes=[0, 1, 2],
        threshold=0.8,
    )
    reader, embedder = _reader(path)

    claim = reader.extract(
        "Every order has a status.", "status", {"table_name": "orders"}
    )

    assert claim is not None
    assert claim.type == "not_null"
    assert claim.column == "status"
    assert claim.source_sentence == "Every order has a status."
    assert embedder.texts == [
        "column: status\ntable: orders\nEvery order has a status."
    ]


def test_reader_rejects_a_missing_head_at_construction(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="head artefact is missing"):
        EmbeddingClaimReader(tmp_path / "missing.npz")


def test_reader_confidence_is_the_argmax_probability(tmp_path: Path) -> None:
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 2.0, 0.0],
        classes=[0, 1, 2],
        threshold=0.5,
    )
    reader, _ = _reader(path)

    claim = reader.extract("Each customer appears once.", "customer_id", {})

    assert claim is not None
    assert claim.confidence == pytest.approx(np.exp(2.0) / (np.exp(2.0) + 2.0))


def test_runtime_loads_the_exact_embedding_revision(
    monkeypatch, tmp_path: Path
) -> None:
    """A moving model tag would make warning coverage drift without attribution."""
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 2.0, 0.0],
        classes=[0, 1, 2],
        threshold=0.5,
    )
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Backend:
        @staticmethod
        def SentenceTransformer(name: str, **kwargs: Any) -> _FakeEmbedder:
            calls.append((name, kwargs))
            return _FakeEmbedder([1.0])

    monkeypatch.setattr(reader_module, "import_module", lambda _: _Backend)

    EmbeddingClaimReader(path)._embedder_for_runtime()

    assert calls == [
        (
            reader_module._MODEL_NAME,
            {
                "revision": reader_module._MODEL_REVISION,
                "trust_remote_code": True,
            },
        )
    ]


def test_reader_identity_fingerprints_every_runtime_input(tmp_path: Path) -> None:
    """A warning should name the exact reader that proposed its query."""
    path = _head(
        tmp_path / "head.npz",
        intercept=[0.0, 2.0, 0.0],
        classes=[0, 1, 2],
        threshold=0.5,
    )

    identity = EmbeddingClaimReader(path, embedder=_FakeEmbedder([1.0])).identity

    assert identity["model"] == reader_module._MODEL_NAME
    assert identity["revision"] == reader_module._MODEL_REVISION
    assert identity["head_sha256"]
    assert identity["threshold"] == 0.5
