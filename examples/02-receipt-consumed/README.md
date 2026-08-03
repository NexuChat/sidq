# Receipt consumed by a separate process

This is a live DataHub proof, run on 2026-07-28 against `localhost:8080`.
The disposable asset is `urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)`.

The receipt body is queryable `sidq.*` structured properties; it records
`commit_sha`, `policy_hash`, and a context hash as provenance. Reproducing the
decision also requires the captured change, graph context, configuration, source
evidence, and tool versions. The writer owns only that namespace and the
`sidq:verified` / `sidq:blocked` badges.

> **The `policy_hash` in the transcript below is historical, and that is the
> point of publishing it.** The scripts compute the hash live from
> `src/sidq/policy/default_policy.yaml`, so every policy change gives a new one.
> This run predates the constraint-reconciliation and `partial_blast_radius`
> rules, so re-running it today yields a different `policy_hash` — a receipt
> pinned to a policy that has since changed is exactly what the independent
> `sidq verify <urn>` CLI path is built to detect. `search_verified` uses the
> separate Sidq MCP verification store; it does not read DataHub Receipts. The
> transcript is kept verbatim rather than rewritten, because editing a recorded
> live run to match today's code would destroy the only thing it proves.
> `tests/test_receipt.py` fails if this paragraph is removed while the transcript
> hash is stale.

To reproduce, make `mcp-server-datahub` available on `PATH`, set
`TOOLS_IS_MUTATION_ENABLED=true` (the receipt caller sets it for its MCP child
process), then run:

```bash
.venv/bin/python scripts/bootstrap_sidq_properties.py
.venv/bin/python examples/02-receipt-consumed/prepare_asset.py
.venv/bin/python examples/02-receipt-consumed/write_receipt.py
.venv/bin/python examples/02-receipt-consumed/read_receipt.py
.venv/bin/python examples/02-receipt-consumed/change_schema.py
.venv/bin/python examples/02-receipt-consumed/read_stale.py
```

For the supported receipt-consumer surface, run `sidq verify <urn>` from a
separate process. Sidq's own MCP server has exactly three tools:
`check_change`, `verify_context`, and `search_verified`; receipt status is not a
fourth MCP tool.

## Captured live output

The bootstrap was first run against a fresh graph and created the complete
schema and the two required visible tags:

```json
{
  "created": [
    "urn:li:structuredProperty:sidq.verdict",
    "urn:li:structuredProperty:sidq.reason_code",
    "urn:li:structuredProperty:sidq.commit_sha",
    "urn:li:structuredProperty:sidq.checked_at",
    "urn:li:structuredProperty:sidq.policy_hash",
    "urn:li:structuredProperty:sidq.rules_fired",
    "urn:li:structuredProperty:sidq.verifier",
    "urn:li:structuredProperty:sidq.evidence_url",
    "urn:li:tag:sidq:verified",
    "urn:li:tag:sidq:blocked"
  ],
  "existing": []
}
```

The deterministic policy engine then produced a PASS and the official MCP
mutation tools acknowledged all three receipt writes:

```json
{
  "add_structured_properties": {
    "message": "Successfully added 8 structured propert(ies) to 1 entit(ies)",
    "success": true
  },
  "add_tags": {
    "message": "Successfully added 1 tag(s) to 1 entit(ies)",
    "success": true
  },
  "receipt": {
    "checked_at": "2026-07-28T22:43:33Z",
    "commit_sha": "receipt-proof-commit",
    "evidence": [],
    "evidence_url": "urn:li:document:shared-4eebf764-8ffd-4066-8b22-002d8d868be5",
    "policy_hash": "09047cb616bbff703b8156594009b39cbf2531ba0d53050e3d3e17e81eed9356",
    "reason_code": null,
    "rules_fired": [],
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)",
    "verdict": "PASS",
    "verifier": "sidq@0.1.0"
  },
  "save_document": {
    "author": "__datahub_system",
    "message": "Successfully created document: Sidq PASS receipt for urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)",
    "success": true,
    "urn": "urn:li:document:shared-4eebf764-8ffd-4066-8b22-002d8d868be5"
  }
}
```

`read_receipt.py` starts a different MCP process. It read the persisted
structured properties rather than the writer response:

```json
{
  "checked_at": "2026-07-28T22:43:33Z",
  "commit_sha": "receipt-proof-commit",
  "evidence_url": "urn:li:document:shared-4eebf764-8ffd-4066-8b22-002d8d868be5",
  "policy_hash": "09047cb616bbff703b8156594009b39cbf2531ba0d53050e3d3e17e81eed9356",
  "reason_code": null,
  "rules_fired": [],
  "stale": false,
  "stale_reason": null,
  "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)",
  "verdict": "PASS",
  "verifier": "sidq@0.1.0"
}
```

The same independent MCP `get_entities` call returned the visible DataHub tag
and the receipt fields. These are the actual response fragments:

```json
{
  "structuredProperties": {
    "properties": [
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.checked_at"}, "values": [{"stringValue": "2026-07-28T22:43:33Z"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.evidence_url"}, "values": [{"stringValue": "urn:li:document:shared-4eebf764-8ffd-4066-8b22-002d8d868be5"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.reason_code"}, "values": [{"stringValue": ""}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.verifier"}, "values": [{"stringValue": "sidq@0.1.0"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.verdict"}, "values": [{"stringValue": "PASS"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.commit_sha"}, "values": [{"stringValue": "receipt-proof-commit"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.policy_hash"}, "values": [{"stringValue": "09047cb616bbff703b8156594009b39cbf2531ba0d53050e3d3e17e81eed9356"}]},
      {"structuredProperty": {"urn": "urn:li:structuredProperty:sidq.rules_fired"}}
    ]
  },
  "tags": {
    "tags": [
      {
        "tag": {
          "properties": {"description": "Sidq receipt badge: verified.", "name": "verified"},
          "urn": "urn:li:tag:sidq:verified"
        }
      }
    ]
  }
}
```

Finally, the disposable schema was changed after the receipt:

```text
schema updated: added receipt_proof_marker
```

The separate reader used the graph schema's `lastModified` timestamp to compute
the stale state; it does not write `stale` anywhere:

```json
{
  "checked_at": "2026-07-28T22:43:33Z",
  "commit_sha": "receipt-proof-commit",
  "evidence_url": "urn:li:document:shared-4eebf764-8ffd-4066-8b22-002d8d868be5",
  "policy_hash": "09047cb616bbff703b8156594009b39cbf2531ba0d53050e3d3e17e81eed9356",
  "reason_code": null,
  "rules_fired": [],
  "stale": true,
  "stale_reason": "asset schema changed after the last verification",
  "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)",
  "verdict": "PASS",
  "verifier": "sidq@0.1.0"
}
```
