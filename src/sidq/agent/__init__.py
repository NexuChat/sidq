"""The autonomous catalog-truth auditor.

It chooses where to point the deterministic engine; it never decides truth.
"""

from sidq.agent.auditor import (
    AuditRun,
    CatalogAuditor,
    Target,
    audit,
    render,
)

__all__ = ("AuditRun", "CatalogAuditor", "Target", "audit", "render")
