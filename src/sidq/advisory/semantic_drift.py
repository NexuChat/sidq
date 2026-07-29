"""Heuristic semantic-drift screen for opt-in, non-blocking evidence.

Similarity is a retrieval heuristic, not a calibrated probability.  In
particular, ``SIDQ_ADVISORY_THRESHOLD`` (default ``0.35``) is an operator-tuned
screening threshold, not a confidence claim.  Every result remains advisory and
is deliberately outside Sidq's deterministic policy engine.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sidq.advisory.embedder import MODEL_ID, embed_texts
from sidq.models import Evidence, Finding

DEFAULT_THRESHOLD = 0.35
_LOG = logging.getLogger(__name__)
_WORD = re.compile(r"\b\w+\b")
_threshold_errors: set[str] = set()

type Embedder = Callable[..., Any | None]


@dataclass(frozen=True, slots=True)
class ColumnUsage:
    """All already-collected context needed to assess one touched column."""

    asset_urn: str
    column_name: str
    catalog_description: str | None
    downstream_consumers: tuple[str, ...] = ()
    sql_expression: str | None = None
    sibling_column_names: tuple[str, ...] = ()

    @property
    def subject(self) -> str:
        return f"{self.asset_urn}#{self.column_name}"

    def usage_context(self) -> str:
        """Render a stable plain-text document from deterministic engine facts."""
        parts = [f"Column: {self.column_name}"]
        if self.downstream_consumers:
            parts.append(
                "Downstream consumers: " + ", ".join(sorted(self.downstream_consumers))
            )
        if self.sql_expression:
            parts.append(f"SQL expression: {self.sql_expression}")
        if self.sibling_column_names:
            parts.append(
                "Sibling columns: " + ", ".join(sorted(self.sibling_column_names))
            )
        return "\n".join(parts)


def advisory_threshold(environ: dict[str, str] | None = None) -> float:
    """Read a safe advisory threshold without ever making a check fail."""
    value = (environ or os.environ).get("SIDQ_ADVISORY_THRESHOLD")
    if value is None or not value.strip():
        return DEFAULT_THRESHOLD
    try:
        threshold = float(value)
    except ValueError:
        _threshold_log_once(value)
        return DEFAULT_THRESHOLD
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        _threshold_log_once(value)
        return DEFAULT_THRESHOLD
    return threshold


def _threshold_log_once(value: str) -> None:
    if value not in _threshold_errors:
        _threshold_errors.add(value)
        _LOG.warning(
            "Ignoring invalid SIDQ_ADVISORY_THRESHOLD=%r; using %s",
            value,
            DEFAULT_THRESHOLD,
        )


def _has_meaningful_description(description: str | None) -> bool:
    return description is not None and len(_WORD.findall(description)) >= 4


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(float(item) ** 2 for item in left))
    right_norm = math.sqrt(sum(float(item) ** 2 for item in right))
    if not math.isfinite(dot) or not left_norm or not right_norm:
        return None
    score = dot / (left_norm * right_norm)
    return score if math.isfinite(score) else None


class SemanticDriftCheck:
    """Compare documented meaning with already-known use; never decide policy."""

    def __init__(
        self,
        *,
        embedder: Embedder = embed_texts,
        threshold: float | None = None,
    ) -> None:
        self._embedder = embedder
        self._threshold = advisory_threshold() if threshold is None else threshold

    def collect(self, columns: Iterable[ColumnUsage]) -> list[Finding]:
        """Return ordered advisory findings, or none if embeddings are unavailable."""
        candidates = sorted(
            (
                column
                for column in columns
                if _has_meaningful_description(column.catalog_description)
            ),
            key=lambda column: (column.column_name, column.asset_urn),
        )
        if not candidates:
            return []
        descriptions = [column.catalog_description or "" for column in candidates]
        contexts = [column.usage_context() for column in candidates]
        try:
            description_vectors = self._embedder(descriptions, side="query")
            context_vectors = self._embedder(contexts, side="document")
        except Exception as error:  # noqa: BLE001 - injected advisory helpers also fail open
            _LOG.warning(
                "Sidq semantic-drift embeddings unavailable: %s", type(error).__name__
            )
            return []
        if (
            description_vectors is None
            or context_vectors is None
            or len(description_vectors) != len(candidates)
            or len(context_vectors) != len(candidates)
        ):
            return []
        findings: list[Finding] = []
        for column, description_vector, context_vector in zip(
            candidates, description_vectors, context_vectors, strict=True
        ):
            similarity = _cosine(description_vector, context_vector)
            if similarity is None or similarity >= self._threshold:
                continue
            evidence = Evidence(
                "semantic_drift",
                column.subject,
                {
                    "advisory": True,
                    "model": MODEL_ID,
                    "similarity": round(similarity, 4),
                    "threshold": self._threshold,
                },
            )
            findings.append(
                Finding(
                    "semantic_drift",
                    "advisory",
                    (
                        f"Column {column.column_name} has low semantic similarity "
                        f"to its observed usage ({similarity:.4f})."
                    ),
                    (evidence,),
                )
            )
        return findings


def collect_if_enabled(
    columns: Iterable[ColumnUsage],
    *,
    environ: dict[str, str] | None = None,
    check: SemanticDriftCheck | None = None,
) -> list[Finding]:
    """Run only for an explicit opt-in; callers append results after policy.

    This deliberately has no graph or database dependency.  Its sole input is
    context a deterministic caller already collected.
    """
    if (environ or os.environ).get("SIDQ_ADVISORY") != "1":
        return []
    return (check or SemanticDriftCheck()).collect(columns)
