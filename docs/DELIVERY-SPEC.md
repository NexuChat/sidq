# DELIVERY SPEC — historical delivery record

This document records the delivery decision made on 2026-07-28. It is
**historical and superseded**, not the current implementation contract. The
current runbook is [`../README.md`](../README.md) and the reproducible connected
setup is [`SETUP.md`](SETUP.md).

## Current state

The current judge-facing landing at <https://sidq.mlki.app> is a dynamic hosted
application. Its **Run it here** actions execute a fixed allowlist of real Sidq
commands on the host; the browser shows a progress timer while the server waits,
then receives bounded captured output. The actions accept no arbitrary command
or request input. DataHub is separately hosted at
<https://datahub.mlki.app>.

The current local journeys are deliberately separate:

- Offline: `make gate-demo` self-bootstraps and replays committed evidence with
  no DataHub.
- Connected: `make mcp-install`, `make demo-stack`, `make mcp-smoke`, then
  `make live-loop` against DataHub OSS.

The repository demo Compose file supplies only controlled PostgreSQL. It relies
on an already-running DataHub and `datahub_network`; it is not a standalone
DataHub deployment.

## Historical problem

The rules require *"a working project link judges can easily try."* Our DataHub runs in
docker **on our machine**. A GitHub Action on GitHub's runners cannot reach it, and
standing up the full quickstart inside a free runner (six containers, several GB) is slow
and fragile. If we discover this late, the hero surface has no home.

## Prior landing-page proposal (superseded)

**Problem found:** `https://sidq.mlki.app` is live, but it serves the **DataHub UI** —
the sponsor's own product, not ours. A judge clicking our "working project link" lands on a
DataHub login screen. That does not satisfy the requirement in spirit and it burns the
strongest thirty seconds we will ever get with a judge.

**Resolution proposed at the time:** a minimal page at the root of that hostname:

- two lines on what Sidq is
- **one real verdict**, rendered from the canonical verdict JSON the engine already emits
  (not a mock, not a screenshot)
- three links: the sealed PRs · the live DataHub, with the demo credentials stated plainly ·
  the repo

DataHub moves to a path or a second hostname. This page is fed by an artifact we already
produce, so it costs close to nothing.

That deliberately static proposal was later superseded by the current dynamic
hosted landing described above. Its fixed command allowlist preserves the
original security boundary while providing a runnable product surface.

## Prior delivery plan

### 1. Real sealed PR threads (the primary judge artifact)

A public demo repo containing **genuine pull requests that already carry the bot's
comments**, produced by running the gate here against the live local DataHub and posting
through the GitHub API. The threads are permanent, readable without installing anything,
and every verdict in them was computed against a real graph.

At minimum four PRs, each one a demonstration scene:

| PR | Change | Expected verdict |
|---|---|---|
| #1 | adds a column, touches nothing downstream | `PASS` + receipt |
| #2 | drops `customers.cust_email`, which has cross-team downstream consumers | `BLOCK` — `critical_downstream`; supporting `wide_blast_radius` warning |
| #3 | references a column that exists in the live DB but **not** in the catalog | `BLOCK` — `STALE_CONTEXT` (the catalog is lying) |
| #4 | the fix for #2, re-run | `PASS` + receipt, and the receipt is read back |

The PR body of each states what a judge is looking at. This is the highest-value, lowest-risk
artifact we can ship, and it costs a few hours.

**Which graph each scene runs on — settled by `docs/RECON.md`, 2026-07-28.** The recon
found the `showcase-ecommerce` pack already carries **844 fine-grained (column-level)
lineage edges across 32 datasets**, spanning Snowflake → dbt → Looker/Power BI/Tableau.
That is far better than anything we would have emitted ourselves, and it is the *judges'
own sample data*. So the demo uses two graphs, each for what it is actually good at:

**Scenes 1, 2, 4 — the real showcase graph.** This exact path exists in it today:

```
snowflake …customers.cust_email      (tagged urn:li:tag:b2fd91.PII_Data — a REAL PII tag)
  → dbt …customers.cust_email
  → snowflake …analytics.order_details.cust_email
  → looker …view.order_details.cust_email
  → looker …explore.order_details.order_details.cust_email
  → chart looker …dashboard_elements.221
  → dashboard looker …dashboards.53
```

A PR touching the dbt model `order_entry/customers.sql` resolves to
`urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)`,
and the blast gate then walks **real** column lineage into a **real** dashboard.
The `critical_downstream` rule blocks because the blast evidence records cross-team
downstream owners. The 16-consumer count supports that decision with a
`wide_blast_radius` warning, while the **real** PII tag is sensitivity context only;
the built-in change path does not emit `pii_exposure` from it. Nothing is staged.
Our demo dbt project must therefore mirror the showcase's dbt model paths so the
resolver lands on those URNs.

**Scene 3 — our own live Postgres.** The showcase pack is metadata-only: there is no
database behind it to ALTER, so `STALE_CONTEXT` cannot be demonstrated on it. Our
`demo/` Postgres exists for exactly this one job. Best form: name our database, schema and
tables so they back one of the **12 postgres datasets already in the showcase graph** —
then the catalog genuinely describes our live database, `make demo-break` renames a column
in it, and Gate 0 catches the catalog lying about an asset the judges can see in the UI.

Standing rule: every rule we show in the video must have a real subject in the graph. A
rule that only fires on a hand-made fixture does not belong in the demo.

### 2. One-command local reproduction

The original combined command was retired because it blurred the offline replay,
the controlled source, and the external DataHub dependency. The supported paths
are now:

```bash
make gate-demo

# Connected, after installing Python 3.12, Docker/Compose, and uv:
make mcp-install
make demo-stack
make mcp-smoke
make live-loop
```

The first path has no DataHub. The second uses DataHub OSS plus the repository's
controlled PostgreSQL source; the repository Compose file does not create the
DataHub graph.

### 3. GitHub Action in fixture mode (optional, honest)

A workflow that runs on any PR opened against the demo repo, using the **replay graph
fixtures** rather than a live DataHub, so a curious judge who opens their own PR still gets
an answer. The comment must say plainly that it ran in fixture mode and that the sealed PRs
above were computed against a live graph. **Never let a fixture-mode run look like a live
one** — the whole project is about not lying about provenance; the delivery layer does not
get an exemption.

## Decisions recorded at the time

- **Cloudflare Tunnel to local GMS was rejected.** Workable — we own the infrastructure — but it makes
  the judged artifact depend on this machine being up during Aug 17–31 judging. A dead
  tunnel at judging time is a lost submission. Rejected on availability risk, not effort.
- **Hosting DataHub was initially rejected.** That decision is superseded: the
  current delivery includes the hosted DataHub and dynamic landing named above.
- **A self-hosted runner was rejected.** Same availability risk as the tunnel.

## Historical owner action

Which GitHub account/org hosts the two public repos — the main `sidq` repo and the
demo repo? Automated tooling owns every git write, but creating a public repo and pushing needs the
owner's `gh` auth to be pointed at the right account. This note is retained only
as context for the earlier plan; it is not a current setup instruction.
