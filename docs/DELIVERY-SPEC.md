# DELIVERY SPEC — the judge-facing surfaces (binding)

Authority: the project design contract §8 item 4. Written 2026-07-28 to kill a risk that would
otherwise surface on day 9.

## The problem

The rules require *"a working project link judges can easily try."* Our DataHub runs in
docker **on our machine**. A GitHub Action on GitHub's runners cannot reach it, and
standing up the full quickstart inside a free runner (six containers, several GB) is slow
and fragile. If we discover this late, the hero surface has no home.

## The decision — three surfaces, no tunnel, no hosting

### 1. Real sealed PR threads (the primary judge artifact)

A public demo repo containing **genuine pull requests that already carry the bot's
comments**, produced by running the gate here against the live local DataHub and posting
through the GitHub API. The threads are permanent, readable without installing anything,
and every verdict in them was computed against a real graph.

At minimum four PRs, each one a scene from the project design contract §6:

| PR | Change | Expected verdict |
|---|---|---|
| #1 | adds a column, touches nothing downstream | `PASS` + receipt |
| #2 | drops `customers.email`, which a downstream dashboard depends on | `BLOCK` — `critical_downstream` + `pii_exposure` |
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
and the blast gate then walks **real** column lineage into a **real** dashboard, with a
**real** PII tag firing `pii_exposure`. Nothing is staged. Our demo dbt project must
therefore mirror the showcase's dbt model paths so the resolver lands on those URNs.

**Scene 3 — our own live Postgres.** The showcase pack is metadata-only: there is no
database behind it to ALTER, so `STALE_CONTEXT` cannot be demonstrated on it. Our
`demo/` Postgres exists for exactly this one job. Best form: name our database, schema and
tables so they back one of the **12 postgres datasets already in the showcase graph** —
then the catalog genuinely describes our live database, `make demo-break` renames a column
in it, and Gate 0 catches the catalog lying about an asset the judges can see in the UI.

Standing rule: every rule we show in the video must have a real subject in the graph. A
rule that only fires on a hand-made fixture does not belong in the demo.

**Known gap (accepted):** the OSS MCP server reports `Data Quality Tools DISABLED` and
does not advertise assertion tools, so the assertion-dependency gate has no MCP path.
It stays where the project design contract §8 already put it — in the cut list — and if built at all it
goes through the Python SDK, not MCP.

### 2. One-command local reproduction

For a judge who wants to run it:

```bash
make demo-up && make demo-ingest && make gate-demo
```

Documented in the README with expected output pasted, so the judge can compare. This is
what makes the project *real* rather than recorded.

### 3. GitHub Action in fixture mode (optional, honest)

A workflow that runs on any PR opened against the demo repo, using the **replay graph
fixtures** rather than a live DataHub, so a curious judge who opens their own PR still gets
an answer. The comment must say plainly that it ran in fixture mode and that the sealed PRs
above were computed against a live graph. **Never let a fixture-mode run look like a live
one** — the whole project is about not lying about provenance; the delivery layer does not
get an exemption.

## Explicitly rejected

- **Cloudflare Tunnel to local GMS.** Workable — we own the infrastructure — but it makes
  the judged artifact depend on this machine being up during Aug 17–31 judging. A dead
  tunnel at judging time is a lost submission. Rejected on availability risk, not effort.
- **Hosting DataHub anywhere.** Cost, time, and no scoring benefit.
- **A self-hosted runner.** Same availability risk as the tunnel.

## Owner action required (blocking for wave 4, not before)

Which GitHub account/org hosts the two public repos — the main `sidq` repo and the
demo repo? Automated tooling owns every git write, but creating a public repo and pushing needs the
owner's `gh` auth to be pointed at the right account. Nothing else in the build is blocked
on this, so it can be answered any time before wave 4.
