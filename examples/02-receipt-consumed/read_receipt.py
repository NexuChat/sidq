"""Separate process: consume the receipt through MCP, then inspect its UI badge."""

from __future__ import annotations

import json
import os

from sidq.policy.engine import PolicyEngine
from sidq.receipt import StdioMCPReceiptToolCaller, get_verification_status

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"


def main() -> None:
    caller = StdioMCPReceiptToolCaller(
        command=os.environ.get("SIDQ_MCP_SERVER", "mcp-server-datahub")
    )
    try:
        policy_hash = (
            PolicyEngine().decide((), commit_sha="receipt-proof-commit").policy_hash
        )
        print(
            json.dumps(
                get_verification_status(URN, caller, current_policy_hash=policy_hash),
                indent=2,
                sort_keys=True,
            )
        )
        print(
            json.dumps(
                caller("get_entities", {"urns": [URN]}), indent=2, sort_keys=True
            )
        )
    finally:
        caller.close()


if __name__ == "__main__":
    main()
