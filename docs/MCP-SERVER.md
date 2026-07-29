# Sidq MCP server

`sidq-mcp` turns Sidq’s deterministic engine into an agent capability. It exposes
exactly three tools over stdio. There are no LLM calls in the server or its truth
checks.

Every response is emitted twice by MCP: as `structuredContent` for clients and as
canonical, minified JSON text for compatibility. The text is produced by
`sidq.serialization.canonical_json`, so identical inputs and verification state
produce byte-identical JSON.

## Connect an MCP client

Install the project, then add this exact `.mcp.json` shape to any MCP-capable agent or
client that accepts the standard MCP server configuration:

```json
{
  "mcpServers": {
    "sidq": {
      "type": "stdio",
      "command": "sidq-mcp",
      "args": [],
      "env": {
        "DATAHUB_GMS_URL": "http://localhost:8080",
        "SIDQ_REPO_ROOT": "/absolute/path/to/your/data-repository",
        "SIDQ_POSTGRES_DSN": "postgresql://sidq:sidq@localhost:55432/warehouse"
      }
    }
  }
}
```

`DATAHUB_GMS_URL` selects the DataHub graph. `SIDQ_REPO_ROOT` is where Sidq
discovers dbt manifests and model SQL. `SIDQ_POSTGRES_DSN` enables
`schema_drift` through the local `psql` client; when it is absent, that check is
reported in `unverifiable`.

Verification history defaults to
`$SIDQ_REPO_ROOT/.sidq/mcp-verifications.json`. Set
`SIDQ_VERIFICATION_STORE` to put the canonical store elsewhere. If the
repository has more than one manifest model, set `SIDQ_SQL_PATH` to the model
path used by raw `sql` calls, for example `models/customers.sql`.

## `check_change`

Answers: “May the agent propose this data-code change?” Call it before writing
or proposing SQL. Pass exactly one of:

- `diff`: a unified diff containing one or more changed, manifest-mapped SQL
  files.
- `sql`: the complete proposed SQL for the sole manifest model, or the model
  selected by `SIDQ_SQL_PATH`.

The existing engine runs end to end: resolver, graph gates, then policy. Gate
logic is not duplicated in the MCP layer. A graph failure returns an explicit
`GRAPH_UNAVAILABLE` error and never grants permission.

Example request:

```json
{
  "name": "check_change",
  "arguments": {
    "sql": "select customer_id, email from raw.customers"
  }
}
```

Example successful response:

```json
{
  "commit_sha": "sha256:0c11d9c3d6e2f8f15e97d842d621702cab0e8bb5d5f912b6b98f0f730ed9d94b",
  "decision": "BLOCK",
  "findings": [
    {
      "evidence": [
        {
          "detail": {
            "field_path": "email"
          },
          "graph_links": [],
          "kind": "unknown_field",
          "subject": "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customers,PROD)#email"
        }
      ],
      "message": "Referenced field is not present in the catalog: urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customers,PROD)#email.",
      "rule_id": "unknown_field",
      "severity": "block"
    }
  ],
  "policy_hash": "d9a9f6e14f6cf16ef3e9d88c8c859d01b13d7f113e460117d7ec82cc5e0fac19",
  "reason_code": null,
  "touched": [
    {
      "added_fields": [
        "customer_id",
        "email"
      ],
      "referenced_fields": [],
      "removed_fields": [],
      "resolution_strategy": "dbt_manifest",
      "source_path": "models/customers.sql",
      "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)"
    }
  ]
}
```

The example is expanded for readability; the wire text is canonical and
minified.

## `verify_context`

Answers: “Is the catalog telling the truth about this asset right now?” Call it
before trusting catalog metadata or querying an asset.

Sidq runs the truth checks available for the asset:

- `schema_drift`: catalog schema versus the live PostgreSQL source.
- `lineage_rot`: stored column lineage versus the local model SQL.

`truthful` is `true` only when both checks complete without findings. A missing
live source, model SQL, or column-level lineage is named in `unverifiable`;
Sidq does not turn an unperformed check into a clean bill of health.

Example request:

```json
{
  "name": "verify_context",
  "arguments": {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)"
  }
}
```

Example response:

```json
{
  "checked_at": "2026-07-28T12:00:00Z",
  "findings": [
    {
      "detail": {
        "claimed_edge": {
          "source_column": "email",
          "source_dataset": "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customers,PROD)",
          "target_column": "email"
        },
        "computed_edges": [],
        "confidence": "high",
        "sql_expression": null
      },
      "graph_links": [],
      "kind": "lineage_rot_missing",
      "subject": "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)#email"
    }
  ],
  "truthful": false,
  "unverifiable": [],
  "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)"
}
```

## `search_verified`

Answers: “Which matching assets have fresh evidence that their catalog context
is truthful?” Call it when selecting data for analysis or generated code.

Only fresh, truthful assets appear in `verified`. The response also preserves
the distinctions an agent needs:

- `unverified`: never checked.
- `stale`: checked, but outside `max_age_days`.
- `unverifiable`: a required truth check could not be completed.
- `rejected`: checked within the window and found untruthful.

Example request:

```json
{
  "name": "search_verified",
  "arguments": {
    "query": "customers",
    "max_age_days": 7
  }
}
```

Example response:

```json
{
  "max_age_days": 7,
  "query": "customers",
  "rejected": [],
  "unverified": [
    {
      "reason": "never_checked",
      "status": "unverified",
      "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customers,PROD)"
    }
  ],
  "verified": [
    {
      "checked_at": "2026-07-28T12:00:00Z",
      "truthful": true,
      "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customers,PROD)",
      "verdict": "TRUTHFUL"
    }
  ]
}
```

If DataHub search fails, the response includes
`error.code = "GRAPH_UNAVAILABLE"` and empty result lists. Clients must treat
that as a failed check, not as “no matching assets.”
