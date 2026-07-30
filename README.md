# Sidq

[![CI](https://github.com/NexuChat/sidq/actions/workflows/ci.yml/badge.svg)](https://github.com/NexuChat/sidq/actions/workflows/ci.yml)

> Everyone is building agents that read the metadata graph and trust it blindly. Sidq is the only one asking whether the graph is lying — and it stops the agent before it builds on the lie.

Sidq is a DataHub-native verification layer for agents and data-code changes. It checks whether catalog context is truthful before an agent relies on it, then applies an explicit policy and leaves evidence that the next agent can read.

## Judge runbook

Four commands, in order of how much they need. The first needs nothing at all.

| # | Command | Needs | What it proves | Takes |
|---|---|---|---|---|
| 1 | `make gate-demo` | nothing — no DataHub, no network, no credentials | The published `BLOCK` verdict is re-derived from the committed graph recording, byte-identical, with the same `policy_hash`. Hand-editing an artifact fails this. | ~2s |
| 2 | `make check` | nothing | 414 tests, lint, format, types — everything CI runs, including the guards on every claim this README makes. | ~15s |
| 3 | `make live-loop` | a running DataHub ([`docs/SETUP.md`](docs/SETUP.md)) | The whole agent loop over the **official MCP server only**: read → decide → write a receipt → a *separate process* reads it back → an asset the audit never reached returns `NOT VERIFIED`. | ~60s |
| 4 | `make repair-demo` | the same DataHub | The repair agent proposes a fix from catalog evidence, re-runs the deterministic engine against the catalog that fix *would* create, and shows what it proved and what it refused. | ~40s |

Nothing above is a recording. If you have no DataHub, run 1 and 2 — they are the
ones that prove determinism, and they need nothing but a clone and `make`.

You can also run row 1, a live catalog audit, and row 4's dry run from the
hosted page without cloning anything: [sidq.mlki.app](https://sidq.mlki.app) has
**Run it here** buttons that execute the real commands on the host and print the
real output. The runnable set is a closed table with no request input, and a
test asserts it contains nothing that can write to a catalog.

**What to look at if you only have five minutes.** Run `make gate-demo`, then open
[`examples/01-blocked-pii-dashboard/verdict.json`](examples/01-blocked-pii-dashboard/verdict.json)
and confirm the printed hash matches the committed one. That single check
establishes the property everything else rests on: the decision is reproducible
and no model participated in it.

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
   - `constraint_reconciliation`: catalog constraint claims versus what the source enforces. The database is the authority on what it enforces; the catalog is a claim about it, and disagreement is the finding. Measured coverage and the honest abstention rate are published in [`docs/RECONCILE-COVERAGE.md`](docs/RECONCILE-COVERAGE.md).
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

## Try it in one command

Point Sidq at a catalog it has never seen. It ranks every asset by how much damage
a lie about it would do — downstream consumers, PII tags, missing ownership,
deprecation — examines the worst first, and tells you what it found *and what it
did not get to*.

```bash
.venv/bin/sidq audit --server http://localhost:8080 --budget 40
```

Run against a DataHub carrying the shipped `showcase-ecommerce` sample, it
perceives 93 entities and 964 lineage edges, chooses its own order, and finds
**all 285 `lineage_field_missing` contradictions inside its first 40 assets** —
because it starts with the one carrying 500 downstream consumers rather than with
whatever the catalog happens to list first.

It also reports unowned consumed assets, and that count is worth reading
carefully: the audit published in
[`examples/03-catalog-truth-report/report.json`](examples/03-catalog-truth-report/report.json)
found 29 across the 82 `showcase-ecommerce` entities it was scoped to, while a
live run here reports 31 because this DataHub also carries the bundled demo
project. Same check, different catalog contents. The published figure is the one
scoped to the sample.

`--write-receipts` carries each verdict back into the catalog as a queryable
`sidq.*` receipt, so the next agent can read what this one concluded. It is off by
default: an audit does not mutate a catalog unless you ask it to.

### The audit that resumes — the catalog is the agent's memory

Those receipts are not only a record; they are state. `--resume` reads them back
before planning: an asset whose receipt still holds — same policy hash, not aged
out, schema unchanged since it was written — is skipped, and the whole budget
flows to assets no run has reached. Coverage converges run over run under a
budget that never changed, and because the memory lives in the catalog rather
than in a file beside the agent, any Sidq instance resumes where any other
stopped. There is no ledger to sync; the catalog is the ledger.

```bash
make converge-demo    # both runs, over official MCP, against the live catalog
```

Or by hand — the second command spends the same budget the first did, on what
the first could not reach:

```bash
.venv/bin/sidq audit --server http://localhost:8080 --budget 40 --write-receipts
.venv/bin/sidq audit --server http://localhost:8080 --budget 40 --write-receipts --resume
```

Nothing is skipped on trust. The reader recomputes the receipt's validity itself,
with the same judgment every receipt consumer uses; a receipt that is stale,
records a `BLOCK`, or cannot be read at all fails that check and the asset goes
back in the queue — if the receipts are unreadable, the prior is empty and
everything is re-examined. Forgetting costs budget, never correctness. And the
report says `vouched`, never `verified`, for anything it skipped: whose word you
are taking is part of the answer.

### The whole loop, on the official MCP server, in one command

`--server` above reads through the DataHub Python SDK. `--via-mcp` reads through
`mcp-server-datahub` instead — the same server any DataHub agent would use, and
nothing else:

```bash
make live-loop        # needs a running DataHub; see docs/SETUP.md
```

Four steps, one process boundary in the middle of them:

1. **Read** through official MCP only — `search`, `get_entities`,
   `list_schema_fields`, `get_lineage`.
2. **Decide** with the shipped policy, and **write** the receipts back through the
   official MCP mutation tools.
3. **Read it back from a separate process**, which recomputes staleness and
   reaches its own verdict. A writer that reports its own success proves nothing.
4. **Ask about an asset the audit never reached** — and get `NOT VERIFIED`, not
   silence. "We did not check" and "we checked and it passed" are the two answers
   this project exists to keep apart.

Step 4 is enforced in the code, not just in the demo. MCP returns column lineage
only when asked for one named column, so field lineage costs one call per column
and the run can only afford a subset. Every asset outside that budget is recorded
as unresolved, reported as `NOT established` rather than `verified clean`, and
**gets no receipt at all** — because the policy treats unverifiable evidence as
informational, so an unexamined asset would otherwise have been stamped `PASS`.

### The repair agent — it proves its fixes before it writes them

Finding a contradiction is half the job. `sidq repair` proposes what to do about
each one, and — more usefully — reports what it cannot fix.

```bash
make repair-demo      # dry run; `sidq repair --via-mcp --apply` writes it
```

Proposals come from catalog evidence only. Two of the six checks are mechanically
repairable, because the correct value already exists elsewhere in the catalog: a
PII marker that lineage says should have propagated, and an owner that every owned
upstream agrees on. The other four produce nothing, each with its reason recorded.
An agent with an answer for all six would be inventing four of them.

**Nothing is offered for writing until the deterministic engine has re-run against
the catalog the repair would create.** It must resolve the finding, introduce no
new one, and the surviving set must still hold when applied together.

That gate changed the design rather than decorating it. The first PII repair
tagged only the column the finding named. Against the live showcase catalog the
engine refused it:

```
Refused — proposed, then disproved:
  order_details#customer_id
    resolves the finding but introduces 1 new one(s)
    would introduce: pii_leak_untagged on …looker…explore.order_details#customer_id
```

A one-hop repair does not fix a leak, it moves it. The proposal it offers instead
covers the whole field-lineage closure — 7 columns across dbt, Snowflake and
Looker — as a single MCP call, and *that* one the engine proves.

## Four surfaces

### 1. The catalog auditor — an agent, not a script

`src/sidq/agent/` decides what to examine from what it has already observed. The
audit script in `examples/03-catalog-truth-report/` reads everything in
enumeration order and behaves identically whatever it finds; the auditor behaves
differently on a different catalog, spends a bounded budget where it matters, and
reports its own coverage gaps rather than presenting a partial sweep as complete.

It never decides truth. It runs the same deterministic engine every other surface
runs and chooses where to point it, so the LLM-free guarantee stays structural
rather than becoming a promise. `examples/05-agent-that-stops/` shows the other
side of the same idea: an analytics agent that asks `verify_context` before it
writes SQL, and stops when the answer is that the catalog cannot be trusted.

### 2. MCP server

`sidq-mcp` exposes exactly three tools over stdio:

| Tool | Question it answers |
|---|---|
| `verify_context(urn)` | Is the catalog telling the truth about this asset right now? |
| `check_change(diff)` | May an agent propose this data-code change? |
| `search_verified(query)` | Which matching assets have fresh, truthful catalog evidence? |

`search_verified` distinguishes `verified`, `unverified`, `stale`, `unverifiable`, and `rejected`; a failed graph lookup is `GRAPH_UNAVAILABLE`, not an empty search result. MCP responses use canonical JSON, so identical inputs and verification state produce byte-identical output. See [`docs/MCP-SERVER.md`](docs/MCP-SERVER.md) for the client configuration and wire examples.

### 3. CLI

The CLI is the canonical engine surface. It accepts a diff or SQL file and emits human-readable or canonical JSON output:

```bash
.venv/bin/sidq check --file demo/dbt/models/customer_revenue.sql --json
.venv/bin/sidq check --diff BASE..HEAD --json
.venv/bin/sidq explain catalog_reality_mismatch
```

Exit codes are `0` for `PASS`, `1` for `WARN`, and `2` for `BLOCK`. The JSON verdict is the artifact consumed by the MCP server, PR bot, and receipt path.

### 4. GitHub PR bot

The bot renders deterministic findings, provenance, graph evidence, impact paths, and the exact reproduction command. This is the real rendered output from [`examples/01-blocked-pii-dashboard/pr-comment.md`](examples/01-blocked-pii-dashboard/pr-comment.md):

<!-- sidq-pr-bot:sticky -->
# 🚫 BLOCKED — <code>pii_exposure</code>, <code>critical_downstream</code>

> **Provenance: LIVE DATAHUB.** Evidence was read from the live graph.

## Deterministic policy decision

Only the deterministic policy findings in this section affect the merge decision.

### 🚫 <code>pii_exposure</code> — BLOCK

**Why:** PII exposure is not permitted for dbt · order_entry_db.order_entry.customers.cust_email.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers.cust_email</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- Changed column: <code>cust_email</code>
- PII tags: <code>tag · PII_Data</code>
- PII tag not carried by **10 downstream consumers**, including <code>Looker view · order-entry-looker.view.order_details</code>, <code>Looker explore · order-entry.explore.order_details</code>, <code>Power BI · datahub_order_entries.Customer_Analytics_Measures</code>

### 🚫 <code>pii_exposure</code> — BLOCK

**Why:** PII exposure is not permitted for dbt · order_entry_db.order_entry.customers.cust_email.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers.cust_email</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- Changed column: <code>cust_email</code>
- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

### ⚠️ <code>wide_blast_radius</code> — WARN

**Why:** This change affects 16 downstream consumers for dbt · order_entry_db.order_entry.customers.

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

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

**Evidence:** [<code>dbt · order_entry_db.order_entry.customers</code>](https://datahub.mlki.app/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cb2fd91.order_entry_db.order_entry.customers%2CPROD%29)

- PII tags: <code>tag · PII_Data</code>
- Reaches: <code>Looker dashboard · dashboards.53</code>
- Blast radius: **16 downstream consumers** within 3 hops
- Column-level impact path:

  <code>dbt · order_entry_db.order_entry.customers.cust_email</code> → <code>dbt · ORDER_ENTRY_DB.analytics.order_details.cust_email</code> → <code>Snowflake · order_entry_db.analytics.order_details.cust_email</code> → <code>Looker view · order-entry-looker.view.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details.order_details.cust_email</code> → <code>Looker explore · order-entry.explore.order_details</code> → <code>Looker chart · dashboard_elements.224</code> → <code>Looker dashboard · dashboards.53</code>
- Path note: Column lineage is proven through the BI field; chart and dashboard hops are entity-level.

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

Reproducibility: <code>policy_hash=baa612f729a56ff7497718cc3cf77cd9142967cb4ec0e075c2b3495eeb2f2927</code> · <code>commit_sha=5addb753788935d4d1aa6a9483c28c6fc124e5c7</code> · run <code>sidq check --diff 5addb753788935d4d1aa6a9483c28c6fc124e5c7^..5addb753788935d4d1aa6a9483c28c6fc124e5c7 --json</code>
