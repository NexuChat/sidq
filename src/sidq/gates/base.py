"""Gate contract: collect facts; policy is the sole decision-maker."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from sidq.graph.client import GraphClient
from sidq.models import Evidence, TouchedAsset


@runtime_checkable
class Gate(Protocol):
    id: str

    def collect(
        self, change: Sequence[TouchedAsset], graph: GraphClient
    ) -> list[Evidence]: ...


def graph_unavailable(subject: str, error: Exception) -> Evidence:
    """Represent an unavailable catalog as evidence, never as a fail-open exception."""
    return Evidence("graph_unavailable", subject, {"error": type(error).__name__})
