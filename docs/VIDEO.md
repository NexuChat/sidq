# Final three-minute film runbook

Target length: 2:45. Record the public landing page and a terminal at 1440x900.
Use the real hosted buttons; do not substitute screenshots or pre-rendered output.

## 00:00-00:20 — the problem

**Picture:** Open `https://sidq.mlki.app`, hold on the thesis, then scroll through
the one-picture architecture.

**Voice:** “Agents increasingly read metadata graphs before they write data code.
But the graph can be stale, internally contradictory, or missing the evidence an
agent needs. Sidq asks the question before the agent acts: is this context true?”

## 00:20-00:55 — the winning moment

**Picture:** Scroll to **Do not take our word for it**. Click **Prove the agent
handoff — read receipt memory**. Keep the real response visible long enough to
read `VERIFIED`, `checked at`, the policy hash, and the evidence document.

**Voice:** “Agent A verified an asset and left a receipt in DataHub. Agent B is a
separate reader with no private handoff state. It reads the receipt from the
catalog, recomputes policy, schema, and age freshness, and returns VERIFIED. The
catalog is not just input or output. It is durable, queryable memory for every
agent.”

## 00:55-01:25 — refusal with evidence

**Picture:** Click **Offline verdict**. Hold on `BLOCK`, `pii_exposure`, the 16
consumers, commit SHA, and policy hash. Scroll back to the visual lineage path.

**Voice:** “For a real data-code change, Sidq follows the changed column through
dbt, Snowflake, Looker, and a live dashboard. PII reaches sixteen downstream
consumers, so the deterministic policy blocks the proposal. Same graph recording,
same policy, same commit: byte-identical verdict. No model participates in this
permission decision.”

## 01:25-01:55 — the catalog contradicts itself

**Picture:** Show the sample statistics, then click **Catalog audit** and keep the
finding summary visible.

**Voice:** “This is not a synthetic claim. A read-only audit of DataHub's shipped
showcase examined all 67 datasets and found 285 internal lineage contradictions,
concentrated in five assets, plus consumed assets with no owner. Where source SQL
was absent, Sidq said unverifiable. It never converted missing evidence into a
green check.”

## 01:55-02:20 — useful action, bounded authority

**Picture:** Click **Repair agent**, then show the proven six-column closure and
the ‘nothing was written’ line. Click **Measured claims** and show the WARN.

**Voice:** “The repair agent rejects plausible fixes that move the leak and offers
only the closure the same engine can re-prove. The public demo is dry-run only.
Sidq can also compile field documentation to bounded read-only SQL: models may
propose what to test, but only measured evidence can influence a warning.”

## 02:20-02:45 — close with proof

**Picture:** Show the five buttons, the GitHub link, CI badge, and live DataHub
link. End on “Evidence before confidence.”

**Voice:** “Sidq is Apache-2.0, DataHub-native, and usable from CLI, CI, or MCP.
The public page runs five real read-only paths, the repository carries every
verdict and benchmark it quotes, and CI re-derives the claims. Do not make the
agent smarter. Make its context provable.”

## Recording rules

- Do not speed up terminal output or hide waits with cuts that imply an instant run.
- Use no copyrighted music. Silence is acceptable.
- Keep the cursor visible for every live click.
- Record a fresh handoff receipt immediately before filming so `checked at` is current.
- Re-run `make gate-demo` and `make check` before capture; stop if either fails.
- The Devpost video must remain public and no longer than three minutes.
