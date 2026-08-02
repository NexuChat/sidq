"""Immutable, proposed assertions read from catalog documentation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ClaimType = Literal[
    "unique", "not_null", "accepted_values", "relationships", "expression"
]


@dataclass(frozen=True, slots=True)
class Claim:
    """A checkable statement, always proposed until an explicit catalog workflow accepts it."""

    type: ClaimType
    column: str
    values: tuple[str, ...] | None = None
    expr: str | None = None
    source_sentence: str = ""
    confidence: float = 0.0
    status: Literal["proposed"] = "proposed"
    # Which reader turned the sentence into this claim. It travels with the claim
    # because the two origins earn different treatment downstream: a deterministic
    # reading that could not be tested is still a fact about the run, while a
    # model's reading that could not be tested is only the model's opinion — and
    # opinions are not evidence here. See `sidq.claims.attest`.
    origin: Literal["rule", "model"] = "rule"

    def __post_init__(self) -> None:
        if self.type not in {
            "unique",
            "not_null",
            "accepted_values",
            "relationships",
            "expression",
        }:
            raise ValueError(f"unsupported claim type: {self.type!r}")
        if not isinstance(self.column, str) or not self.column:
            raise ValueError("claim column must be a non-empty string")
        if not isinstance(self.source_sentence, str) or not self.source_sentence:
            raise ValueError("claim source_sentence must be a non-empty string")
        if (
            not isinstance(self.confidence, (int, float))
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("claim confidence must be between 0 and 1")
        if self.values is not None:
            if isinstance(self.values, str):
                raise ValueError("claim values must be a sequence of strings")
            values = tuple(self.values)
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError("claim values must contain non-empty strings")
            object.__setattr__(self, "values", values)
        if self.expr is not None and (
            not isinstance(self.expr, str) or not self.expr.strip()
        ):
            raise ValueError("claim expr must be a non-empty string when supplied")
        if self.origin not in {"rule", "model"}:
            # An unrecognised origin would fall through the admissibility check
            # in `attest` as though it were deterministic. Refuse it at the door.
            raise ValueError(f"unsupported claim origin: {self.origin!r}")
