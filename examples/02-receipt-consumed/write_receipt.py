"""Run the deterministic policy decision and write its PASS receipt via MCP."""

from __future__ import annotations

import json
import os

from sidq.policy.engine import PolicyEngine
from sidq.receipt import StdioMCPReceiptToolCaller, build_receipt, write_receipt

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"


def main() -> None:
    # This invokes Sidq's deterministic policy engine. No evidence fires a rule,
    # so it produces a real PASS with the hash of the policy actually used.
    verdict = PolicyEngine().decide((), commit_sha="receipt-proof-commit")
    receipt = build_receipt(URN, verdict, verifier="sidq@0.1.0")
    caller = StdioMCPReceiptToolCaller(
        command=os.environ.get("SIDQ_MCP_SERVER", "mcp-server-datahub")
    )
    try:
        print(json.dumps(write_receipt(receipt, caller), indent=2, sort_keys=True))
    finally:
        caller.close()


if __name__ == "__main__":
    main()
