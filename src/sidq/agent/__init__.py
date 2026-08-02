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
from sidq.agent.memory import PriorReceipt, recall
from sidq.agent.writeback import (
    WriteOutcome,
    receipts_for,
    render_writeback,
    write_receipts,
)

__all__ = (
    "AuditRun",
    "CatalogAuditor",
    "PriorReceipt",
    "Target",
    "WriteOutcome",
    "audit",
    "recall",
    "receipts_for",
    "render",
    "render_writeback",
    "write_receipts",
)
