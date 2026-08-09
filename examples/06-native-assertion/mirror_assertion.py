#!/usr/bin/env python3
"""Mirror the captured receipt into DataHub's native assertion surface.

This runs from the project environment directly::

    export DATAHUB_GMS_URL=http://localhost:8080
    export DATAHUB_GMS_TOKEN=...            # omit on an unauthenticated quickstart
    .venv/bin/python examples/06-native-assertion/mirror_assertion.py

No DataHub SDK is involved. The mirror speaks DataHub's documented GraphQL
custom-assertion API (``upsertCustomAssertion`` / ``reportAssertionResult``)
over plain HTTP, which is why the one interpreter a judge already has is
enough. Earlier revisions of this example needed a second, SDK-carrying
interpreter; the git history records that boundary and why it fell.
"""

from __future__ import annotations

import os

from sidq.receipt.assertion import emit_assertions
from sidq.receipt.build import Receipt

# The dataset this example was run against. It carries a quickstart-specific
# instance id, so it will not exist in another catalog: override
# SIDQ_EXAMPLE_URN with any dataset of your own that already holds a receipt.
DATASET_URN = os.environ.get(
    "SIDQ_EXAMPLE_URN",
    "urn:li:dataset:(urn:li:dataPlatform:dbt,"
    "b2fd91.order_entry_db.order_entry.products,PROD)",
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
        evidence_url="urn:li:document:shared-30fd40e4-a6cd-44e3-92bd-e74586c31ec1",
        # Empty evidence is intentional: emit_assertions then reports the
        # whole recorded verdict under sidq.verdict instead of inventing a rule.
        evidence=(),
    )


def main() -> None:
    """Emit the assertion and print the observable idempotency counts."""

    result = emit_assertions([captured_receipt()])
    print(
        f"created={len(result['created'])} "
        f"existing={len(result['existing'])} "
        f"runs={len(result['runs'])} "
        f"retired={len(result['retired'])} "
        f"skipped={len(result['skipped'])}"
    )


if __name__ == "__main__":
    main()
