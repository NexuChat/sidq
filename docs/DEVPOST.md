# Devpost submission copy

## What it does

Sidq is a DataHub-native verification layer for data-code changes and agents. It
asks whether catalog context is truthful before an agent relies on it.

Sidq separates evidence collection from judgment. It compares catalog schema with a
live PostgreSQL source, reconciles the constraints the catalog claims against the
constraints that source actually enforces, checks catalog lineage and descriptions
where the required source material exists, resolves changed SQL to DataHub assets,
follows downstream impact, and checks governance evidence such as PII, ownership,
and deprecation. Its policy engine emits exactly `PASS`, `WARN`, or `BLOCK`. A graph
failure is fail-closed. A deterministic finding is the only kind that can block.

The clearest example is `STALE_CONTEXT`: the live demo renames `raw.customers.email`
to `email_address` without re-ingesting DataHub. Sidq sees the graph and source
disagree and blocks the context rather than letting an agent build on it.

For a code change that removes `cust_email`, Sidq follows real column lineage through
dbt, Snowflake, Looker, and a Looker dashboard. It sees the live `PII_Data` tag and
returns `BLOCK`. The published example records 16 downstream consumers.

The whole loop runs over the official DataHub MCP server in one command,
`make live-loop`: the agent reads with `search`, `get_entities`,
`list_schema_fields`, and `get_lineage`; the policy decides; the receipt is written
with the MCP mutation tools; and a *separate process* reads it back and recomputes
its own verdict. A fourth step asks about an asset the audit never reached and gets
`NOT VERIFIED`, because MCP returns column lineage only per named column, so a
bounded run records what it could not afford to read instead of passing it over in
silence. Assets in that state get no receipt at all.

Sidq also writes a receipt back to DataHub through the official MCP mutation tools.
The receipt has queryable `sidq.*` properties, a visible `sidq:verified` or
`sidq:blocked` tag, and a human-readable evidence document. A separate reader can
check the receipt. The status becomes stale when the asset changes, the receipt ages
past the configured window, or the policy hash changes.

Finally, we audited DataHub’s shipped `showcase-ecommerce` sample using read-only
catalog metadata. The audit found 285 internal field-lineage contradictions across
67 datasets. This is a narrower claim than saying the source systems are broken: the
contradictory claims are visible inside the catalog itself.

## How it was built

The engine is deterministic Python. Gates collect structured evidence. The policy
engine alone decides. The same input, graph snapshot, and policy file produce a
byte-identical verdict identified by `policy_hash` and `commit_sha`.

Sidq has three surfaces:

- A CLI: `.venv/bin/sidq check --file ... --json` or `.venv/bin/sidq check --diff ... --json`.
- A GitHub PR bot that renders the decision, provenance, evidence, impact paths, and reproduction command.
- A stdio MCP server, `sidq-mcp`, with `verify_context`, `check_change`, and `search_verified`.

The shipped judge path contains no LLM calls. Sidq does not replace an agent. It
gives an agent a refusal and verification path before it acts, and leaves evidence
for the next agent.

## Technologies

- Python 3.12 or newer.
- DataHub and the official `mcp-server-datahub` MCP server.
- MCP over stdio for the Sidq server and receipt path.
- `sqlglot` for deterministic SQL parsing and field extraction.
- `PyYAML` for the restricted policy and asset-map formats.
- PostgreSQL for the controlled live-source drift demo.
- Docker Compose for the local demo environment.
- GitHub Actions and the GitHub PR API for the PR bot.

The repository is licensed Apache-2.0. The project code was written during the
submission period.

## Data used

The main audit uses DataHub’s officially shipped `showcase-ecommerce` sample. The
local recon records 67 datasets, 835 stored fine-grained lineage records, and 844
upstream field references. The truth report’s 285 contradictions are catalog-only
comparisons: a stored lineage target names a field absent from its stored target
schema.

The PII and Looker demonstration uses the same showcase graph. Its published
evidence links `customers.cust_email` to Looker dashboard `b2fd91.dashboards.53`
and records the `urn:li:tag:b2fd91.PII_Data` tag.

The `STALE_CONTEXT` demonstration uses the controlled PostgreSQL database in
`demo/`. Its seed contains 36 customers, 72 orders, 144 order items, and the
`analytics.customer_revenue` view. `make demo-break` renames the live column; the
catalog remains unchanged until re-ingestion.

The receipt demonstration uses the disposable asset
`urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)` and the
published `examples/02-receipt-consumed/` scripts. It does not claim that this
disposable asset is part of the showcase sample.

## Limitations

`lineage_rot` is not adjudicable on the shipped showcase sample because the datapack
does not include the original model SQL. Sidq reports all 32 attempts as
`unverifiable`; it does not label them clean or rotten.

The assertion-dependency gate has no MCP path in the open-source DataHub server;
the server reports data-quality tools as disabled. It is not presented as an MCP
capability.

Sidq does not provide a global trust score, continuous background drift sentinel,
blanket write quarantine, or model-assisted blocking. Those boundaries keep the
decision explicit and reproducible.

## Required disclosures

- The project was created during the submission period. Every line of Sidq's own
  source, tests, and documentation was written inside the window; no pre-existing
  project code was carried in.
- AI coding assistants were used during development.
- **Third-party material is included, and it is data rather than code.** The
  claim-extraction corpus under `data/claims/` is mined from permissively licensed
  public sources — dbt repositories (MIT, Apache-2.0, BSD, Unlicense, CC0-1.0),
  SchemaStore (Apache-2.0), FHIR R5 core (CC0-1.0), and application-code and
  error-message corpora (MIT, Apache-2.0, BSD-3-Clause). Every released row retains
  its source path, commit or version, and licence. The graph fixtures under
  `tests/fixtures/graph/` are recordings of DataHub's own shipped
  `showcase-ecommerce` sample. Full provenance is in
  [`data/claims/ATTRIBUTION.md`](../data/claims/ATTRIBUTION.md),
  [`data/claims/NOTICE`](../data/claims/NOTICE), and
  [`data/claims/DATASHEET.md`](../data/claims/DATASHEET.md). Google Discovery JSON
  was deliberately excluded because its redistribution licence could not be
  confirmed.
- The repository is Apache-2.0 licensed.
- The video uses no copyrighted music. It uses silence or a permissively licensed track with attribution.

## Try it

Project link: https://sidq.mlki.app

Live DataHub: https://datahub.mlki.app

Published demo credentials for the live DataHub: username `datahub`, password
`datahub`.

Local reproduction:

```bash
make demo-up && make demo-ingest && make demo-break && .venv/bin/sidq check --file demo/dbt/models/customer_revenue.sql --json
```

The repository also includes the worked verdicts, receipt proof, and catalog truth
report under `examples/`.
