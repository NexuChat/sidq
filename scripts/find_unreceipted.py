"""Pick the asset live-loop's step 4 asks about, honestly.

The Makefile used to freeze one URN as "an asset the audit never reached", and
for an amnesiac audit under a small budget that was even true. Then the audit
learned to resume: coverage converges, run after run, and on 2026-07-31 it
reached the frozen asset and receipted it — turning step 4's assertion into a
stale claim the demo failed on. Rehearsing the runbook on a fresh clone is
what caught it.

What step 4 demonstrates is a property of absence: an asset with no receipt
must come back NOT VERIFIED, never silently clean. So the asset is discovered
at run time — the first, in stable order, that carries no sidq receipt on this
catalog right now. When every dataset in the search page carries one, this
prints ``ALL_COVERED`` instead: that is not a demo failure, it is the
converging audit's finish line, and the Makefile says so rather than aborting.
"""

from __future__ import annotations

import sys

# The parser is the catalog reader's own. Borrowing its private helper is less
# wrong than writing a second parser for the same tool and letting them drift.
from sidq.gates.self_contradiction import _mcp_urns
from sidq.graph.client import StdioMCPToolCaller
from sidq.receipt.read import get_verification_statuses


def main() -> int:
    caller = StdioMCPToolCaller()
    try:
        urns = _mcp_urns(
            lambda query: caller(
                "search",
                {"query": query, "filter": "entity_type = dataset", "num_results": 50},
            ),
            "*",
        )
        if not urns:
            print(
                "sidq: the catalog returned no datasets to ask about", file=sys.stderr
            )
            return 2
        statuses = get_verification_statuses(urns, caller)
        for urn in urns:
            if not statuses.get(urn, {}).get("verdict"):
                print(urn)
                return 0
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()
    print("ALL_COVERED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
