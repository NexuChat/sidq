# Sidq MCP server

`sidq-mcp` turns Sidq’s deterministic engine into an agent capability. It exposes
exactly three tools over stdio. There are no LLM calls in the server or its truth
checks.

Every response is emitted twice by MCP: as `structuredContent` for clients and as
canonical, minified JSON text for compatibility. The text is produced by
`sidq.serialization.canonical_json`, so identical inputs and verification state
produce byte-identical JSON.

## Two MCP servers, two jobs

The official `mcp-server-datahub` is Sidq's graph dependency: Sidq uses its
DataHub read and opt-in mutation tools. `sidq-mcp` is Sidq's own server and
exposes exactly the three tools documented below. Installing one does not
install, configure, or attach the other.

Run the connected smoke path from the Sidq repository root before configuring
an agent:

```bash
cd /absolute/path/to/sidq
make mcp-install
make mcp-smoke
```

The smoke target initializes `sidq-mcp` and requires exactly
`check_change`, `verify_context`, and `search_verified`; it then exercises the
official `mcp-server-datahub` against the live DataHub graph.

## Connect Codex

The current [Codex MCP documentation](https://developers.openai.com/codex/mcp/)
supports stdio servers through `codex mcp add`, configuration in
`.codex/config.toml`, `codex mcp list`, and the interactive `/mcp` status view.

From any shell, register Sidq with absolute paths:

```bash
codex mcp add sidq --env DATAHUB_GMS_URL=http://localhost:8080 --env SIDQ_REPO_ROOT=/absolute/path/to/data-repository -- /absolute/path/to/sidq/.venv/bin/sidq-mcp
codex mcp list
```

Then start Codex in the data repository and enter `/mcp`. The server should list
`check_change`, `verify_context`, and `search_verified`.

For a repository-scoped configuration, create `.codex/config.toml` only in a
trusted repository. Do not publish or store token or DSN values. Export them in
the shell that starts Codex and forward only their variable names:

```toml
[mcp_servers.sidq]
command = "/absolute/path/to/sidq/.venv/bin/sidq-mcp"
cwd = "/absolute/path/to/data-repository"
env_vars = ["DATAHUB_GMS_TOKEN", "SIDQ_POSTGRES_DSN"]

[mcp_servers.sidq.env]
DATAHUB_GMS_URL = "http://localhost:8080"
SIDQ_REPO_ROOT = "/absolute/path/to/data-repository"
```

`DATAHUB_GMS_TOKEN` and `SIDQ_POSTGRES_DSN` are optional. If required in your
environment, set their values outside the repository before launching Codex.
Prefer a secret manager; for a one-shell manual check, read a token without
echoing it, export it, and then launch Codex:

```bash
read -rsp "DataHub GMS token: " DATAHUB_GMS_TOKEN
printf '\n'
export DATAHUB_GMS_TOKEN
codex
```

An HTTP `401` from the DataHub dependency means the token is missing or invalid.
Do not treat that as a completed verification, and do not add the value to the
repository or Codex configuration. An unauthenticated local quickstart does not
need a token.

Install the optional workflow from the root of the target data repository where
Codex will start. The install location is inside that repository, not inside
Sidq:

```bash
cd /absolute/path/to/data-repository
npx skills add NexuChat/sidq --skill datahub-verify --agent codex
```

The resulting path is
`/absolute/path/to/data-repository/.agents/skills/datahub-verify`. Installing the
skill does not attach either MCP server; run `cd /absolute/path/to/sidq` and
`make mcp-smoke` separately.

## Connect another MCP client

Install the project, then add this exact `.mcp.json` shape to any MCP-capable agent or
client that accepts the standard MCP server configuration:

```json
{
  "mcpServers": {
    "sidq": {
      "type": "stdio",
      "command": "/absolute/path/to/sidq/.venv/bin/sidq-mcp",
      "args": [],
      "env": {
        "DATAHUB_GMS_URL": "http://localhost:8080",
        "SIDQ_REPO_ROOT": "/absolute/path/to/your/data-repository"
      }
    }
  }
}
```

`DATAHUB_GMS_URL` selects the DataHub graph. `SIDQ_REPO_ROOT` is where Sidq
discovers dbt manifests and model SQL. `SIDQ_POSTGRES_DSN` enables
`schema_drift` through the local `psql` client; when it is absent, that check is
reported in `unverifiable`. Forward secret values from the client process's
environment instead of committing them to this JSON.

`SIDQ_VERIFICATION_STORE` selects an optional local Sidq MCP verification-record
file that `search_verified` may read. The three read-only tools do not persist to
that file: records produced by `verify_context` remain in the current MCP server
process. This store is not a DataHub Receipt store and `search_verified` is not a
DataHub receipt reader. Use `sidq verify <urn>` for the independent DataHub
Receipt read. If the repository has more than one manifest model, set
`SIDQ_SQL_PATH` to the model path used by raw `sql` calls, for example
`models/customers.sql`.

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
- `constraint_reconciliation`: catalog constraint claims versus the constraints the
  live source actually enforces. The catalog projection is narrow by construction —
  only `nullable=false` becomes a claim, because that is the one enforcement
  assertion the graph seam carries, so keys and check constraints are never
  reconciled in either direction. A claim the source does not enforce is a finding
  (`constraint_contradicts_catalog`). A constraint the source enforces but the
  catalog never claimed is **not** a finding: the catalog is silent, not lying, and
  counting it would mark every ordinary table untruthful.

`truthful` is `true` only when every check that ran completed without findings. A
missing live source, model SQL, column-level lineage, or constraint introspection
is named in `unverifiable`; Sidq does not turn an unperformed check into a clean
bill of health.

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

The classification source is the Sidq MCP verification store populated in the
current server process (or explicitly preloaded from its configured local
store), not DataHub Receipt properties. Every response therefore includes
`"verification_source": "sidq_mcp_store"`. DataHub Receipt consumption is the
separate `sidq verify <urn>` CLI path.

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
  "verification_source": "sidq_mcp_store",
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
