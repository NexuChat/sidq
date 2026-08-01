"""A small classifier that proposes only claims the engine can still verify.

The embedding model reads catalog prose, but this module only turns that reading
into a proposed assertion.  The attester still decides whether the assertion
was tested, and the engine still decides whether it holds.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np

from .models import Claim, ClaimType

_LABELS = (
    "none",
    "unique",
    "not_null",
    "accepted_values",
    "relationships",
    "expression",
)
_PROPOSABLE = ("unique", "not_null")
_MODEL_NAME = "microsoft/harrier-oss-v1-270m"
# This is the exact snapshot used to produce the committed embeddings and head.
# A moving `main` ref would let warning coverage change without any code or
# artifact changing, which is precisely the kind of invisible drift Sidq exists
# to make attributable.
_MODEL_REVISION = "31de22b673913c7d658c0f03f792d77c2dcf8ebd"
_DEFAULT_HEAD_PATH = Path(__file__).resolve().parents[3] / "data/claims/reader/head.npz"


class _Embedder(Protocol):
    def encode(
        self,
        sentences: list[str],
        *,
        show_progress_bar: bool,
        normalize_embeddings: bool,
    ) -> Any: ...


class EmbeddingClaimReader:
    """Keep a trained reader cheap to ship without making it part of judgment.

    The head is plain NumPy data, so merely enabling the optional reader does
    not pull a training stack into normal Sidq installations.  The much larger
    embedding model remains a runtime concern, and is loaded only when prose
    actually needs a model reading.
    """

    def __init__(
        self,
        head_path: str | Path = _DEFAULT_HEAD_PATH,
        *,
        embedder: _Embedder | None = None,
    ) -> None:
        """Refuse an incomplete installation before a catalog run can begin.

        A missing head cannot be treated as a model abstention: it says that the
        installed reader was never made usable, whereas abstention says a usable
        reader considered one particular sentence and declined it.
        """
        self._head_path = Path(head_path)
        if not self._head_path.is_file():
            raise FileNotFoundError(
                "claim reader head artefact is missing: "
                f"{self._head_path}. Run scripts/train_claim_reader.py to create it."
            )
        with np.load(self._head_path) as head:
            self._coef = np.asarray(head["coef"])
            self._intercept = np.asarray(head["intercept"])
            self._classes = np.asarray(head["classes"])
            self._threshold = float(np.asarray(head["threshold"])[0])
        self._embedder = embedder

    @property
    def identity(self) -> dict[str, object]:
        """The complete, stable identity of the reader that proposes queries."""
        return {
            "kind": "embedding-linear-head",
            "model": _MODEL_NAME,
            "revision": _MODEL_REVISION,
            "head_sha256": hashlib.sha256(self._head_path.read_bytes()).hexdigest(),
            "threshold": self._threshold,
        }

    def extract(
        self, sentence: str, column: str, schema_context: Mapping[str, Any]
    ) -> Claim | None:
        """Propose only a high-confidence claim whose predicate needs no arguments.

        The model may identify a sentence worth testing, but it cannot invent
        values or relationships the linear head has no way to specify.  That is
        why even a confident non-proposable label remains an abstention here.
        """
        if (
            not isinstance(sentence, str)
            or not isinstance(column, str)
            or not sentence
            or not column
        ):
            return None

        table = schema_context.get("table_name") or ""
        # This must match train_claim_reader._text exactly: changing even its
        # delimiters silently moves inference away from the embeddings the head saw.
        text = f"column: {column}\ntable: {table}\n{sentence}"
        embedding = np.asarray(
            self._embedder_for_runtime().encode(
                [text], show_progress_bar=False, normalize_embeddings=True
            )
        )[0]
        probabilities = _softmax(self._coef @ embedding + self._intercept)
        index = int(np.argmax(probabilities))
        claim_type = _LABELS[int(self._classes[index])]
        confidence = float(probabilities[index])
        if claim_type not in _PROPOSABLE or confidence < self._threshold:
            return None
        return Claim(
            type=cast(ClaimType, claim_type),
            column=column,
            source_sentence=sentence,
            confidence=confidence,
        )

    def _embedder_for_runtime(self) -> _Embedder:
        if self._embedder is None:
            backend = import_module("sentence_transformers")
            self._embedder = cast(
                _Embedder,
                backend.SentenceTransformer(
                    _MODEL_NAME,
                    revision=_MODEL_REVISION,
                    trust_remote_code=True,
                ),
            )
        return self._embedder


def _softmax(logits: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    shifted = logits - np.max(logits)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials)
