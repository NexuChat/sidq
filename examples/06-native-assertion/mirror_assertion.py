#!/usr/bin/env python3
"""Mirror the captured receipt using an interpreter with ``acryl-datahub``.

The Sidq project venv deliberately does not include the DataHub SDK because of
its measured pydantic conflict with Sidq's MCP dependency. Run this with the
separate Python interpreter that already has ``acryl-datahub``, not
``.venv/bin/sidq``.
"""

from __future__ import annotations

import os

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig

from sidq.receipt.assertion import emit_assertions
from sidq.receipt.build import Receipt

DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
    "b2fd91.order_entry_db.order_entry.customers,PROD)"
)


def captured_receipt() -> Receipt:
    """Return the receipt state that was already accepted by DataHub."""

    return Receipt(
        urn=DATASET_URN,
        verdict="PASS",
        reason_code=None,
        commit_sha="4a07305275945639f6538f85b7fc4450e99cd7ee",
        checked_at="2026-08-01T00:49:35Z",
        policy_hash="baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927",
        rules_fired=(),
        verifier="sidq@0.1.0",
        evidence_url="urn:li:document:shared-9dbb86d7-617f-4f0c-97a7-4d16f3ccfa5f",
        # Empty evidence is intentional: emit_assertions then reports the
        # whole recorded verdict under sidq.verdict instead of inventing a rule.
        evidence=(),
    )


def main() -> None:
    """Emit the assertion and print the observable idempotency counts."""

    gms_url = os.environ["DATAHUB_GMS_URL"]
    gms_token = os.environ["DATAHUB_GMS_TOKEN"]
    # Assertions are not exposed by DataHub's MCP mutation tools, so this
    # explicit SDK client is the narrow boundary needed for this write.
    graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=gms_token))
    try:
        result = emit_assertions([captured_receipt()], graph)
    finally:
        close = getattr(graph, "close", None)
        if callable(close):
            close()

    print(
        f"created={len(result['created'])} "
        f"existing={len(result['existing'])} runs={len(result['runs'])}"
    )


if __name__ == "__main__":
    main()
