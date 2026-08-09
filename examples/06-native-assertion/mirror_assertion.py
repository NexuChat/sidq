#!/usr/bin/env python3
"""Mirror the captured receipt using an interpreter with ``acryl-datahub``.

The Sidq project venv deliberately does not include the DataHub SDK because of
its measured pydantic conflict with Sidq's MCP dependency, so run this with the
separate interpreter that already has ``acryl-datahub`` -- not
``.venv/bin/sidq``. That interpreter will not have ``sidq`` importable either,
so put the source tree on its path explicitly::

    PYTHONPATH=/path/to/sidq/src \\
      /path/to/sdk-interpreter examples/06-native-assertion/mirror_assertion.py
"""

from __future__ import annotations

import os

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig

from sidq.receipt.assertion import emit_assertions
from sidq.receipt.build import Receipt

# The dataset this example was run against. It carries a quickstart-specific
# instance id, so it will not exist in another catalog: override
# SIDQ_EXAMPLE_URN with any dataset of your own that already holds a receipt.
DATASET_URN = os.environ.get(
    "SIDQ_EXAMPLE_URN",
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.order_entry_db.order_entry.addresses,PROD)",
)


def captured_receipt() -> Receipt:
    """Return the receipt state that was already accepted by DataHub."""

    return Receipt(
        urn=DATASET_URN,
        verdict="PASS",
        reason_code=None,
        commit_sha="faab25e9f5ef77f3df36c833b9f6048f21f3e933",
        checked_at="2026-07-30T22:22:58Z",
        policy_hash="baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927",
        rules_fired=(),
        verifier="sidq@0.1.0",
        evidence_url="urn:li:document:shared-4eb640b1-6aa5-4cd2-a184-dcca36d606de",
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
