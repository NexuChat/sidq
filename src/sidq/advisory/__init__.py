"""Opt-in, non-blocking model-assisted checks.

Nothing in this package participates in deterministic policy evaluation.  Callers
must keep its findings separate from the policy decision.
"""

from sidq.advisory.semantic_drift import (
    ColumnUsage,
    SemanticDriftCheck,
    advisory_threshold,
    collect_if_enabled,
)

__all__ = (
    "ColumnUsage",
    "SemanticDriftCheck",
    "advisory_threshold",
    "collect_if_enabled",
)
