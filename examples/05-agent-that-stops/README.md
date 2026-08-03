# 05 — the agent that stops

Same agent. Same goal. Same catalog. The only difference is whether it is allowed
to check.

```bash
examples/05-agent-that-stops/agent.py --blind   # what an agent does today
examples/05-agent-that-stops/agent.py           # the same agent, with Sidq
```

Both runs are offline. They replay the committed graph fixtures under
`tests/fixtures/graph/`, so the transcripts below are what you get, not what we
hope you get.

## Without Sidq

The agent reads the catalog, sees a column named `cust_email`, and writes the
query. This is the normal, reasonable thing to do — and it is the whole problem.

```text
AGENT WITHOUT SIDQ

  → goal: "How many customers can we reach by email in each region?"
  → read catalog schema for customers
    observed: 22 columns, including cust_email
    decision: catalog looks fine — writing the query

--- what the agent produced ---

select region_id, count(cust_email)
from b2fd91.order_entry_db.order_entry.customers
group by region_id
```

Nothing here is careless. The agent asked the catalog, the catalog answered, and
the answer was taken at face value because there is nothing else to take it at.

## With Sidq

Same code path, one addition: it verifies before it proposes. It does not get a
clean bill, and it does not get a lie either. It gets **an answer it cannot
complete** — and that turns out to be enough to stop it.

```text
AGENT WITH SIDQ

  → goal: "How many customers can we reach by email in each region?"
  → search_verified(customers)
    observed: 0 verified, 0 never verified
  → verify_context(b2fd91.order_entry_db.order_entry.customers)
    observed: truthful=False, 0 finding(s), 24 unverifiable
    decision: no finding, but checks could not be completed — refusing to treat an unperformed check as a clean one

--- what the agent produced ---

-- No SQL proposed.
-- Sidq could not confirm the catalog is truthful about b2fd91.order_entry_db.order_entry.customers.
--   not checked: constraint_reconciliation (live source does not expose constraint introspection)
--   not checked: lineage_rot ×22 (graph client cannot retrieve target-side column lineage)
--   not checked: schema_drift (live source is not configured)
-- An unverified catalog is not a safe basis for a query.
```

**Read that refusal precisely, because the honest version is the interesting
one.** Offline, the agent is not stopping because it caught the catalog lying. It
is stopping because three checks could not run at all — there is no live
PostgreSQL source to compare the schema against, and the fixture graph client
cannot serve target-side column lineage. Zero findings were produced.

A less careful demo would call that `truthful: true` and write the SQL. Zero
findings, after all. That is the exact substitution this project exists to
refuse: **"we did not check" is not "we checked and it passed."** The agent's
branch is on `unverifiable`, not only on `findings`, and it names each check it
could not perform instead of quietly dropping it.

To see it stop on a *finding* rather than an abstention, run the connected loop
against a live DataHub — [`docs/SETUP.md`](../../docs/SETUP.md) — where
`schema_drift` and `lineage_rot` can actually be adjudicated. The blocking path
for a proposed change is [`01-blocked-pii-dashboard`](../01-blocked-pii-dashboard/),
where the deterministic engine refuses a real diff on proven cross-team
downstream evidence.

## Why this counts as an agent

It is not LLM-driven, and that is deliberate: the model is what Sidq exists to
stop, so putting one on the decision path would undercut the claim. What makes it
an agent is not who drives it but whether it **chooses its next action from what
it observed**. This one searches, verifies, and then branches — proceeding,
switching to a different asset, or refusing outright depending on what the
catalog turns out to be. A script would emit the same SQL either way; that is
precisely what the `--blind` run is.

The control flow being deterministic also means the same catalog state always
produces the same transcript, so the demo behaves the same under the lights as it
does here.

## What it uses

Sidq's own MCP server, over a real MCP client session — `search_verified` to
prefer assets with verification records, then `verify_context` on the asset it
settles on. Three read-only tools, no mutation path. The graph underneath is the
committed recording of DataHub's own shipped `showcase-ecommerce` sample.
