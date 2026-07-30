# Sidq GitHub PR bot

Sidq turns the engine's deterministic `PASS` / `WARN` / `BLOCK` verdict into one
decision-first pull-request comment and one check run. Re-runs edit the same
sticky comment. They do not add a new comment for every push.

The bot has two provenance modes:

- `live` reads the reachable DataHub graph through the official
  `mcp-server-datahub`. The comment says `LIVE DATAHUB`.
- `fixture` replays recorded graph responses. The comment says
  `FIXTURE REPLAY — NOT LIVE DATAHUB` in a prominent banner.

There is no automatic live-to-fixture fallback. Choosing replay is explicit so
a connectivity failure can never silently become fixture-backed evidence.

## Install in a repository

The runner needs Python 3.12 or newer and a checkout of the pull-request files.
Pin both third-party actions and Sidq itself to full commit SHAs:

```yaml
name: Sidq

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: read
  issues: write
  checks: write

jobs:
  sidq:
    runs-on: self-hosted
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5
        with:
          persist-credentials: false

      - uses: YOUR_ORG/sidq@SIDQ_FULL_COMMIT_SHA
        env:
          DATAHUB_GMS_URL: https://datahub.example.com
          # Add the authentication environment required by your DataHub MCP setup.
        with:
          token: ${{ github.token }}
          mode: live
```

Use a runner that can reach `DATAHUB_GMS_URL`. A GitHub-hosted runner cannot
reach a DataHub instance bound only to a laptop or private Docker network. In
live mode the action installs the pinned official
`mcp-server-datahub==0.6.0`; set `datahub-mcp-command` when the runner provides
the executable another way.

For fixture replay:

```yaml
      - uses: YOUR_ORG/sidq@SIDQ_FULL_COMMIT_SHA
        with:
          token: ${{ github.token }}
          mode: fixture
          fixture-dir: .sidq/fixtures/graph
```

If `fixture-dir` is empty, the action uses Sidq's shipped demo graph fixtures.
Those fixtures describe the demo assets; a real repository should record and
version its own fixture set.

The public demo uses `pull_request_target` so forked PRs can receive comments.
That event supplies a write-capable token, so its workflow deliberately keeps
the executable Sidq action on the immutable base SHA and checks the untrusted
PR into a separate directory as data only. Copy
`.github/workflows/sidq-demo.yml` as a whole if fork support is needed. Never
install or execute code from the PR checkout in that workflow.

## Permissions

| Permission | Access | Why |
|---|---:|---|
| `contents` | read | Check out the files to inspect |
| `pull-requests` | read | Resolve all changed files through the PR Files API |
| `issues` | write | Create or update the PR's single sticky issue comment |
| `checks` | write | Publish the explicit Sidq check conclusion |

GitHub downgrades `GITHUB_TOKEN` for forked `pull_request` workflows, so the
basic workflow cannot write a comment or check for an untrusted fork. Use the
isolated `pull_request_target` pattern in the demo workflow when that behavior
is required.

The engine exit code maps to the check conclusion:

| Verdict | Engine exit | Check conclusion |
|---|---:|---|
| `PASS` | `0` | `success` |
| `WARN` | `1` | `neutral` |
| `BLOCK` | `2` | `failure` |

The composite action converts `WARN` into a GitHub workflow warning after it
has created the neutral check; `BLOCK` fails the action step.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `token` | required | Token for PR files, comments, and checks |
| `mode` | `live` | `live` or `fixture` |
| `fixture-dir` | shipped demo fixtures | Recorded graph fixture directory |
| `policy` | Sidq default policy | Policy path relative to `repo-root` |
| `repo-root` | `.` | Checkout to inspect, relative to `GITHUB_WORKSPACE` |
| `datahub-mcp-command` | installed official server | Custom MCP executable path |

With an empty `policy` input, the action reads the default policy from the
pinned Sidq action checkout. A custom policy must be a file below `repo-root`;
paths and symlinks that resolve outside that checkout are rejected.

Every comment includes the policy hash, immutable head commit SHA, and the
exact `sidq check --diff BASE...HEAD --json` command for local reproduction.
The renderer escapes human-readable evidence and only turns recorded HTTP(S)
`graph_links` into DataHub links.

## Worked BLOCK comment

This is the byte-for-byte renderer output for
`examples/01-blocked-pii-dashboard/verdict.json`. It is also saved as
`examples/01-blocked-pii-dashboard/pr-comment.md`.

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
