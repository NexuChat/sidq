"""Separate process: prove schema change makes the persisted receipt stale."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import SchemaMetadataClass
from prepare_asset import URN

from sidq.policy.engine import PolicyEngine
from sidq.receipt import StdioMCPReceiptToolCaller, get_verification_status


def main() -> None:
    graph = DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("SIDQ_DATAHUB_UI_URL", "http://localhost:8080")
        )
    )
    schema = graph.get_aspect(URN, SchemaMetadataClass)
    assert schema is not None and schema.lastModified is not None
    schema_modified_at = schema.lastModified.time
    caller = StdioMCPReceiptToolCaller(
        command=os.environ.get("SIDQ_MCP_SERVER", "mcp-server-datahub")
    )
    try:
        policy_hash = (
            PolicyEngine().decide((), commit_sha="receipt-proof-commit").policy_hash
        )
        print(
            json.dumps(
                get_verification_status(
                    URN,
                    caller,
                    current_policy_hash=policy_hash,
                    schema_modified_at=datetime.fromtimestamp(
                        schema_modified_at / 1000, UTC
                    ),
                ),
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        caller.close()


if __name__ == "__main__":
    main()
