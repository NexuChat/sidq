# Sidq

> Everyone is building agents that read the metadata graph and trust it blindly. Sidq is the only one asking whether the graph is lying — and it stops the agent before it builds on the lie.

Sidq is a DataHub-native verification layer for agents and data-code changes. It checks whether catalog context is truthful before an agent relies on it, then applies an explicit policy and leaves evidence that the next agent can read.

## The problem is already in the sample

We scanned DataHub's own shipped `showcase-ecommerce` sample using read-only catalog metadata. It contains **285 internal contradictions across 67 datasets**, plus **29 consumed-but-unowned assets**. This is not a claim that DataHub's source systems are broken. It is a narrower, hand-checkable finding: the catalog contains claims that contradict other claims visible in the catalog. A curated, officially shipped sample already contains this much inconsistency; nothing in the sample checks for it before an agent builds on it.

One example is `powerbi … Customer_Analytics_Measures`. Its stored schema has exactly two fields: `Value` and `Customer LTV`. It nevertheless carries **58 column-lineage edges**, **57 of which target fields that do not exist** in that schema. The complete evidence is in [`docs/TRUTH-REPORT.md`](docs/TRUTH-REPORT.md) and [`examples/03-catalog-truth-report/report.json`](examples/03-catalog-truth-report/report.json).

The negative result matters too. `lineage_rot` could not be adjudicated on this sample because the datapack ships no model SQL. All **32/32** attempts were `unverifiable`; Sidq refused to call them rot without a code-versus-catalog comparison. A tool that knows when to stay silent is the product.

## What Sidq does

Sidq separates evidence collection from judgment:

1. **Truth checks** compare catalog claims with reality or with each other:
   - `schema_drift`: catalog schema versus a live PostgreSQL source.
   - `lineage_rot`: stored column lineage versus local model SQL. Missing SQL is `unverifiable`, not clean and not rot.
   - `doc_rot`: descriptions that refer to columns no longer present.
   - catalog self-contradictions such as a lineage target field missing from its stored target schema.
2. **Change gates** resolve changed SQL/dbt files to DataHub assets, check referenced datasets and fields, calculate downstream blast radius and paths, and apply governance evidence such as PII, ownership, and deprecation where available.
3. **The policy engine** turns evidence into exactly `PASS`, `WARN`, or `BLOCK`. A graph failure is fail-closed: it becomes evidence and cannot grant permission.

The boundary is explicit. The deterministic findings are the only findings that can produce `BLOCK`. An advisory finding can produce `WARN` only and can never turn `PASS` into `BLOCK`:

```json
{
  "decision": "WARN",
  "deterministic_findings": ["only these can block"],
  "advisory_findings": ["never blocks"]
}
```

The shipped judge path contains no LLM calls. A future model-assisted advisory lane may help with meaning-level checks, but it remains advisory and does not decide policy. The same policy and the same commit produce a byte-identical verdict, identified by `policy_hash` and `commit_sha`.

## Three surfaces

### 1. MCP server

`sidq-mcp` exposes exactly three tools over stdio:

| Tool | Question it answers |
|---|---|
| `verify_context(urn)` | Is the catalog telling the truth about this asset right now? |
| `check_change(diff)` | May an agent propose this data-code change? |
| `search_verified(query)` | Which matching assets have fresh, truthful catalog evidence? |

`search_verified` distinguishes `verified`, `unverified`, `stale`, `unverifiable`, and `rejected`; a failed graph lookup is `GRAPH_UNAVAILABLE`, not an empty search result. MCP responses use canonical JSON, so identical inputs and verification state produce byte-identical output. See [`docs/MCP-SERVER.md`](docs/MCP-SERVER.md) for the client configuration and wire examples.

### 2. CLI

The CLI is the canonical engine surface. It accepts a diff or SQL file and emits human-readable or canonical JSON output:

```bash
.venv/bin/sidq check --file demo/dbt/models/customer_revenue.sql --json
.venv/bin/sidq check --diff BASE..HEAD --json
.venv/bin/sidq explain catalog_reality_mismatch
```

Exit codes are `0` for `PASS`, `1` for `WARN`, and `2` for `BLOCK`. The JSON verdict is the artifact consumed by the MCP server, PR bot, and receipt path.

### 3. GitHub PR bot

The bot renders deterministic findings, provenance, graph evidence, impact paths, and the exact reproduction command. This is the real rendered output from [`examples/01-blocked-pii-dashboard/pr-comment.md`](examples/01-blocked-pii-dashboard/pr-comment.md):

<!-- sidq-pr-bot:sticky -->
# 🚫 BLOCKED — <code>pii_exposure</code>, <code>critical_downstream</code>

> **Provenance: LIVE DATAHUB.** Evidence was read from the live graph.

## Deterministic policy decision

Only the deterministic policy findings in this section affect the merge decision.

### 🚫 <code>pii_exposure</code> — BLOCK

**Why:** PII exposure is not permitted for dbt · order_entry_db.order_entry.customers.cust_email.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers.cust_email</code>](http://localhost:9002/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- Changed column: <code>cust_email</code>
- PII tags: <code>tag · PII_Data</code>
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.221</code> → <code>Looker dashboard · dashboards.53</code>

### ⚠️ <code>wide_blast_radius</code> — WARN

**Why:** This change affects 16 downstream consumers for dbt · order_entry_db.order_entry.customers.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](http://localhost:9002/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.221</code> → <code>Looker dashboard · dashboards.53</code>

<details>
<summary>Downstream consumers (16)</summary>

- <code>Looker chart · dashboard_elements.221</code>
- <code>Looker chart · dashboard_elements.222</code>
- <code>Looker chart · dashboard_elements.223</code>
- <code>Looker chart · dashboard_elements.224</code>
- <code>Looker dashboard · dashboards.53</code>
- <code>Looker explore · order-entry.explore.order_details</code>
- <code>Looker view · order-entry-looker.view.order_details</code>
- <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>
- <code>Power BI · datahub_order_entries.Essential_KPI_Measures</code>
- <code>Power BI · datahub_order_entries.Geographic_Measures</code>
- <code>Power BI · datahub_order_entries.ORDER_DETAILS</code>
- <code>Power BI · datahub_order_entries.Product_Perfromance_Measures</code>
- <code>Power BI · datahub_order_entries.Time_Inteligence_Measures</code>
- <code>Snowflake · order_entry_db.analytics.order_details</code>
- <code>Snowflake · order_entry_db.analytics.order_details_replica</code>
- <code>dbt · ORDER_ENTRY_DB.analytics.order_details</code>

</details>

<details>
<summary>Cross-team owners (9)</summary>

- <code>group · 1e0398a3-113f-475e-b6fc-32ab72a634d2</code>
- <code>group · ORG_BACKEND_ENG</code>
- <code>user · alex@example.com</code>
- <code>user · brock1@example.com</code>
- <code>user · bryan@example.com</code>
- <code>user · jonny2@example.com</code>
- <code>user · kirk@example.com</code>
- <code>user · marty@example.com</code>
- <code>user · sam@example.com</code>

</details>

### 🚫 <code>critical_downstream</code> — BLOCK

**Why:** This change has critical or cross-team downstream consumers for dbt · order_entry_db.order_entry.customers.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](http://localhost:9002/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.221</code> → <code>Looker dashboard · dashboards.53</code>

<details>
<summary>Downstream consumers (16)</summary>

- <code>Looker chart · dashboard_elements.221</code>
- <code>Looker chart · dashboard_elements.222</code>
- <code>Looker chart · dashboard_elements.223</code>
- <code>Looker chart · dashboard_elements.224</code>
- <code>Looker dashboard · dashboards.53</code>
- <code>Looker explore · order-entry.explore.order_details</code>
- <code>Looker view · order-entry-looker.view.order_details</code>
- <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>
- <code>Power BI · datahub_order_entries.Essential_KPI_Measures</code>
- <code>Power BI · datahub_order_entries.Geographic_Measures</code>
- <code>Power BI · datahub_order_entries.ORDER_DETAILS</code>
- <code>Power BI · datahub_order_entries.Product_Perfromance_Measures</code>
- <code>Power BI · datahub_order_entries.Time_Inteligence_Measures</code>
- <code>Snowflake · order_entry_db.analytics.order_details</code>
- <code>Snowflake · order_entry_db.analytics.order_details_replica</code>
- <code>dbt · ORDER_ENTRY_DB.analytics.order_details</code>

</details>

<details>
<summary>Cross-team owners (9)</summary>

- <code>group · 1e0398a3-113f-475e-b6fc-32ab72a634d2</code>
- <code>group · ORG_BACKEND_ENG</code>
- <code>user · alex@example.com</code>
- <code>user · brock1@example.com</code>
- <code>user · bryan@example.com</code>
- <code>user · jonny2@example.com</code>
- <code>user · kirk@example.com</code>
- <code>user · marty@example.com</code>
- <code>user · sam@example.com</code>

</details>

---

Reproducibility: <code>policy_hash=09047cb616bbff703b8156594009b39cbf2531ba0d53050e3d3e17e81eed9356</code> · <code>commit_sha=d3f3bd2f4fe31837867592162ccea08859be6947</code> · run <code>sidq check --diff d3f3bd2f4fe31837867592162ccea08859be6947^..d3f3bd2f4fe31837867592162ccea08859be6947 --json</code>

## Why Sidq is not its neighbours

Impact analysis already exists. The *decision* does not, and nothing checks whether the catalog itself is telling the truth.

| Neighbour | Why Sidq is not it |
|---|---|
| **[`acryldata/dbt-impact-action`](https://github.com/acryldata/dbt-impact-action)** | Converts dbt to URNs and shows blast radius. Impact analysis exists; the decision does not, and it does not check catalog truth. |
| Monte Carlo · Foundational · Atlan | Closed commercial products focused on code checks; they do not check the catalog against the live source for every PR. |
| Datafold · Recce | Data diff and impact analysis; not Sidq's governance policy and catalog-truth receipt. |
| Atlas · Flyway · Prisma | Live schema drift in CI, but schema registries/migration tools without lineage, dashboards, or PII context. |
| DataHub circuit breaker | Rejects a pipeline run, not a code merge or an agent's proposed change. |
| Metadata Tests · Schema Assertions | Compare against ingested or declared expectations; they are not this catalog-truth check at PR time. |

## Quickstart

From a fresh checkout with the documented Python and Docker prerequisites:

```bash
make demo-up && make demo-ingest && .venv/bin/sidq check --file demo/dbt/models/customer_revenue.sql --json
```

The controlled source demo and its `email` → `email_address` drift cycle are documented in [`demo/README.md`](demo/README.md). The full environment setup is [`docs/SETUP.md`](docs/SETUP.md). For a test-only verification run, use `.venv/bin/pytest -q`.

Live surfaces:

- [Sidq landing page](https://sidq.mlki.app)
- [Live DataHub](https://datahub.mlki.app) — username `datahub`, password `datahub`

## Roadmap and out of scope

Sidq deliberately does not claim these as built:

- **Continuous drift sentinel:** out of scope because this submission verifies context at the point an agent reads or proposes a change; a background service would expand the operational surface.
- **Blanket write quarantine:** out of scope because Sidq does not own every producer or write path in a DataHub deployment; it fails closed at its decision points instead.
- **Global trust score:** out of scope because a single aggregate score hides which check was missing, stale, or contradicted. Sidq exposes evidence and statuses instead.
- **Model-assisted advisory lane:** not part of the deterministic judgment. If added, models may produce advisory `WARN` findings only; they never block.

The assertion-dependency gate has no MCP path in the OSS server: the server reports data-quality tools as disabled. It is therefore not presented as an MCP capability; any future implementation would need the Python SDK path.

## Disclosures

- Built during the submission period.
- AI coding assistants were used during development.
- No pre-existing code was incorporated; the repository's project code was written during the submission period.

## Repository map

- [the project design contract](the project design contract) — v3 product thesis, surfaces, scope, and neighbour map.
- [`docs/TRUTH-REPORT.md`](docs/TRUTH-REPORT.md) — the catalog-only audit and its negative `lineage_rot` result.
- [`docs/MCP-SERVER.md`](docs/MCP-SERVER.md) — MCP configuration and tool contracts.
- [`docs/PR-BOT.md`](docs/PR-BOT.md) — PR action and rendering details.
- [`examples/`](examples/) — worked verdicts and machine-readable evidence.
