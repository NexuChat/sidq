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
from sidq.agent.writeback import (
    WriteOutcome,
    receipts_for,
    render_writeback,
    write_receipts,
)

__all__ = (
    "AuditRun",
    "CatalogAuditor",
    "Target",
    "WriteOutcome",
    "audit",
    "receipts_for",
    "render",
    "render_writeback",
    "write_receipts",
)
