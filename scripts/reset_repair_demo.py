"""Undo the repair agent's PII propagation so the demonstration can be re-run.

`sidq repair --apply` writes a real change to a real catalog, which is the point —
but it also means the contradiction it fixed is gone, and the next person to run
the demo sees nothing to repair. This puts the sample back.

It removes the marker only from columns *downstream* of the one the showcase
sample marks by hand, and never from that column itself. That column is the
evidence the whole repair is derived from; removing it would not reset the demo,
it would delete the fact the demo is about.

The set is re-derived from live lineage rather than from a stored list of what was
written, because a hard-coded undo list drifts the moment the closure changes.
Uses the official MCP `remove_tags` tool, so the reset runs on the same surface as
everything else.
"""

from __future__ import annotations

import sys

from sidq.gates.self_contradiction import CatalogSnapshot
from sidq.graph.client import MCPGraphClient, StdioMCPToolCaller
from sidq.receipt.write import StdioMCPReceiptToolCaller

BUDGET = 15
SOURCE = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.ORDER_ENTRY_DB.analytics.order_details,PROD)"
)
COLUMN = "customer_id"
MARKER = "urn:li:tag:b2fd91.PII_Data"


def main() -> int:
    graph = MCPGraphClient(StdioMCPToolCaller())
    try:
        snapshot = CatalogSnapshot.from_mcp(graph, field_lineage_budget=BUDGET)
    finally:
        close = getattr(graph, "close", None)
        if callable(close):
            close()

    reached: set[tuple[str, str]] = set()
    queue = [(SOURCE, COLUMN)]
    while queue:
        node = queue.pop()
        if node in reached:
            continue
        reached.add(node)
        queue.extend(
            (edge.target_urn, edge.target_field)
            for edge in snapshot.edges
            if edge.source_urn == node[0]
            and edge.source_field == node[1]
            and edge.target_field is not None
        )

    fields = {
        (entity.urn, item.path): item
        for entity in snapshot.entities
        for item in entity.fields
    }
    targets = sorted(
        node
        for node in reached - {(SOURCE, COLUMN)}
        if MARKER in getattr(fields.get(node), "tags", ())
    )
    if not targets:
        print("reset: nothing to undo — the sample already shows the contradiction")
        return 0

    caller = StdioMCPReceiptToolCaller()
    try:
        caller(
            "remove_tags",
            {
                "tag_urns": [MARKER],
                "entity_urns": [urn for urn, _ in targets],
                "column_paths": [column for _, column in targets],
            },
        )
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()
    print(f"reset: removed the propagated marker from {len(targets)} column(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
