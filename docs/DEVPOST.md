# Devpost submission copy

## Submission fields

**Project name:** Sidq

**Tagline:** Provable context before DataHub agents act.

**Primary challenge:** Agents That Do Real Work — Sidq reads DataHub through
MCP, acts on its evidence, writes optional receipts back, and lets the next
agent inherit a verifiable decision.

**Repository:** https://github.com/NexuChat/sidq

**Live project:** https://sidq.mlki.app

**Live DataHub:** https://datahub.mlki.app

**Public video:** <PUBLIC_VIDEO_URL> *(must be the corrected replacement, not the
superseded 175.317-second v2 export)*

**Testing instructions** *(paste into the Devpost Testing instructions field
visible to judges; never into the public description, repository, video, issue,
or screenshot)*:

```text
Open https://datahub.mlki.app and sign in with the read-only Reader account.
Role: Reader (no metadata mutation permission)
Username: <READER_USERNAME>
Password: <READER_PASSWORD>

Then open https://sidq.mlki.app. Run "Run the independent receipt read" first, inspect
the referenced receipt and evidence document in DataHub, and run "Offline
verdict" to re-derive the committed BLOCK result. The public buttons are fixed,
read-only demonstrations and accept no request payload.
```

Replace both placeholders in Devpost immediately before submission. Do not
replace them in this file. The account supplied to judges must have the Reader
role only; verify in DataHub that it cannot edit tags, properties, ownership, or
documentation.

**Evidence links:**

- Reproducible verdict: [`examples/01-blocked-pii-dashboard/verdict.json`](../examples/01-blocked-pii-dashboard/verdict.json)
- Catalog audit and 285/67/5 scope: [`docs/TRUTH-REPORT.md`](TRUTH-REPORT.md)
- Public QA record: [`docs/QA-RESULTS.md`](QA-RESULTS.md)
- Dependency and local setup: [`docs/SETUP.md`](SETUP.md)
- Third-party data provenance: [`data/claims/ATTRIBUTION.md`](../data/claims/ATTRIBUTION.md)

## Why the name

**صِدق** — *sidq* — is the Arabic word for truthfulness: the discipline of
saying only what you can stand behind. The tool is named after the rule it
enforces — a check that could not complete is never reported as clean, a
receipt never claims more than the evidence inside it, and an agent that
cannot confirm the catalog refuses to build on it. The name is the
specification.

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
dbt, Snowflake, Looker, and a Looker dashboard. The `critical_downstream` rule
returns `BLOCK` because the blast evidence records cross-team downstream owners.
The 16-consumer count triggers a supporting `wide_blast_radius` warning, while the
live `PII_Data` tag is sensitivity context only.

The whole loop runs over the official DataHub MCP server in one command,
`make live-loop`: the agent reads with `search`, `get_entities`,
`list_schema_fields`, and `get_lineage`; the policy decides; the receipt is written
with the MCP mutation tools; and a *separate process* reads it back and independently
recomputes whether the recorded Receipt still holds under the current context,
policy, and age. A fourth step asks about an asset carrying no receipt — chosen at
run time, since the resuming audit eventually reaches any asset named in advance —
and gets `NOT VERIFIED`: a bounded run records what it could not afford to read
instead of passing it over in silence, and assets in that state get no receipt at
all.

The audit can also resume from shared current state. With explicit Receipt
writeback, `sidq audit --resume` re-checks the latest receipt values before
planning. A current value steers budget toward an unexamined asset; stale,
missing, or unreadable values return the asset to consideration. A recorded
refusal is current, so it also frees budget — re-deriving the same `BLOCK` every
run is how the tail of a catalog never gets read — and it is reported as
`BLOCKED`, never as a vouch, and it authorizes nothing. The reader keeps those
questions apart by design: whether a receipt applies, what it decided, and what
it permits are three answers, not one boolean. DataHub stores latest values, not
append-only history, and does not provide exactly-once coordination. A skipped
asset is reported as `vouched`, never as verified by this run.

The same mechanism works across space, not only time. Several auditor processes
can work one catalog simultaneously with no message bus, no lock service, no
leader and no shared filesystem. Each worker re-reads a receipt immediately
before examining an asset and writes one immediately after deciding. That
read-before-write sequence narrows the race window but cannot remove it. Each
worker also enters the shared consequence-ranked plan at an offset derived from
its own id, so four processes do not begin on the same asset.

The promise is **at-least-once, never exactly-once**: MCP has no claim or
compare-and-set primitive, so two workers can still examine the same unreceipted
asset. Deterministic duplicate work is safe because both reach the same verdict,
but DataHub exposes the latest receipt rather than an append-only history, so the
observer cannot count collisions after the fact. `make swarm-demo` kills one
worker deliberately; because nothing is assigned, its unreceipted work remains
eligible for any survivor. That is operational behavior, not a measured recovery
total. The separate observer can prove current recognizable non-stale receipts,
current-run receipts, and latest worker attribution only. DataHub is not merely
an input or output to that loop; it is the shared state.

A second agent, `sidq repair`, decides what to do about each finding — and reports
what it cannot fix. Proposals come from catalog evidence only, and nothing is
offered for writing until the deterministic engine has re-run against the catalog
that repair would create: it must resolve the finding, introduce no new one, and
the surviving set must still hold when applied together. In the complete-lineage
regression, that gate refuses the obvious one-column PII fix because it creates a
new untagged consumer downstream; the next proposal covers the whole
field-lineage closure — 6 columns across dbt, Snowflake and Looker — in one MCP
call, and the engine proves it. Four of the six regression checks produce no
proposal at all, each with its reason recorded, because an agent with an answer
for all six would be inventing four. The live showcase is adjudicated separately:
if its lineage evidence is incomplete, the dry run fails closed, rejects the
proposal, and writes nothing instead of borrowing the fixture's proof.

When writeback is explicitly enabled, Sidq attempts a receipt through the official
MCP mutation tools; writeback is off by default and a rejected mutation is reported
rather than treated as stored evidence.

The receipt has queryable `sidq.*` properties, a visible `sidq:verified` or
`sidq:blocked` tag, and a human-readable evidence document. A separate reader can
check the receipt. Staleness covers the current semantic entity metadata plus
complete one-hop upstream and downstream lineage. Sidq's own receipt properties,
badges, and evidence documents are excluded so a successful write does not
invalidate itself. Missing, partial, or error context is stale (fail-closed), and
a policy-hash mismatch invalidates immediately. The CLI default maximum age is 7
days. The hosted public handoff alone uses 45 days solely to span judging through
August 31, 2026; any context or policy change still invalidates immediately.

Finally, we audited DataHub’s shipped `showcase-ecommerce` sample using read-only
catalog metadata. After examining all 67 datasets, the audit surfaced 285 internal
field-lineage contradictions concentrated in 5 assets. This is a narrower claim
than saying the source systems are broken: the contradictory claims are visible
inside the catalog itself.

The new `sidq claims` command takes the same evidence boundary into field
descriptions. It reads a dataset's field descriptions from DataHub through the
official MCP server, turns each documented sentence into a testable claim,
compiles that claim to read-only SQL, runs it against the live PostgreSQL source,
and lets the deterministic policy engine judge the row counts that come back. A
model may decide what to test; only the engine may decide what is true. The
deterministic reader runs first and the model is consulted only on sentences it
declined; a model-proposed claim that could not be tested contributes nothing and
is dropped, so it can never cause a `BLOCK`.

The reader is a linear head over `microsoft/harrier-oss-v1-270m`, a multilingual
embedding model covering 94+ languages, trained on 2,048 rows and evaluated on a
held-out 528. At its operating point it reaches 95.8% precision and 58.0% recall
on 72 proposals, and proposes only the argument-free `unique` and `not_null`
claim types. A gradient-boosted head had the same precision within noise and 16
points worse recall, while adding a training stack to inference. In the live
demo, 6 documented fields produce 4 claims — 3 by rules and 1 by the trained
reader — while 2 sentences are declined by both; one violation is found because
`status` is documented as "One of: pending, paid, fulfilled" while 12 rows are
`refunded`, giving a `WARN` verdict. Details are in
[`docs/CLAIM-READER.md`](CLAIM-READER.md).


## What each check compares

Field identity is resolved rather than string-matched: a column crossing
platforms carries the spelling each imposes (Snowflake upper-cases, dbt
lower-cases, JSON and Avro arrive as `[version=2.0].[type=struct]…` paths), and
Sidq resolves those to one identity so a naming convention is never reported as
a contradiction. Protection is recognised by meaning, not by label — a column
inherited from a `PII` source and marked `GDPR`, `HIPAA` or `Confidential`
satisfies the check, while a bare `not_pii` denial does not. Deprecation is read
both ways DataHub records it, the first-class aspect and the custom-property
form. And a bounded view says so: MCP `search` returns one page, so dangling
edges are adjudicated only against a complete view and the rest are reported
`unverifiable` — a paged reader's boundary is not the catalog's contradiction.

## How it was built

The engine is deterministic Python. Gates collect structured evidence. The policy
engine alone decides. The same input, graph snapshot, and policy file produce a
byte-identical verdict identified by `policy_hash` and `commit_sha`.

Sidq has three surfaces:

- A CLI: `.venv/bin/sidq check --file ... --json` or `.venv/bin/sidq check --diff ... --json`.
- A GitHub PR bot that renders the decision, provenance, evidence, impact paths, and reproduction command.
- A stdio MCP server, `sidq-mcp`, with `verify_context`, `check_change`, and `search_verified`.

`search_verified` classifies matches against the separate Sidq MCP
`VerificationStore`; it is not a DataHub Receipt reader. `sidq verify <urn>` is
the independent consumer that reads the latest Receipt and current context from
DataHub.

The repository includes a cross-agent `datahub-verify` skill, installable with
`npx skills add NexuChat/sidq --skill datahub-verify --agent codex`. It installs
under `.agents/skills/datahub-verify`; the command does not install Sidq or
attach MCP. It is also under public upstream review at
[datahub-project/datahub-skills#81](https://github.com/datahub-project/datahub-skills/pull/81).
That link is a contribution in review, not a claim of merge or endorsement.

The blocking path contains no model calls. The optional documentation reader may
extend `WARN` coverage after deterministic rules abstain, but it can never grant
permission or produce `BLOCK`; its exact model revision, head fingerprint and
threshold are reported with the run. Sidq does not replace an agent. It gives an
agent a refusal and verification path before it acts, and leaves evidence for the
next agent.

## Technologies

- Python 3.12 (the minor version exercised by CI).
- DataHub and the official `mcp-server-datahub` MCP server.
- MCP over stdio for the Sidq server and receipt path.
- `sqlglot` for deterministic SQL parsing and field extraction.
- `PyYAML` for the restricted policy and asset-map formats.
- NumPy plus a pinned multilingual embedding model for the optional documentation
  reader; query results never enter that model.
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

The lineage and sensitivity demonstration uses the same showcase graph. Its published
evidence links `customers.cust_email` to Looker dashboard `b2fd91.dashboards.53`
and records the `urn:li:tag:b2fd91.PII_Data` tag as sensitivity context. The
blocking finding is `critical_downstream`, based on cross-team downstream
ownership; this example does not claim that the change introduced a PII route.

The `STALE_CONTEXT` demonstration uses the controlled PostgreSQL database in
`demo/`. Its seed contains 36 customers, 72 orders, 144 order items, and the
`analytics.customer_revenue` view. `make demo-break` renames the live column; the
catalog remains unchanged until re-ingestion.

The receipt demonstration uses the disposable asset
`urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)` and the
published `examples/02-receipt-consumed/` scripts. It does not claim that this
disposable asset is part of the showcase sample.

## Limitations

The built-in change path has neither a before/proposed route delta nor proof that
a classified source field participates in a proposed route. It therefore does not
emit `pii_exposure` or `access_policy_conflict`. The policy can still recognize
those evidence kinds when a caller supplies explicit, proven route evidence.

`lineage_rot` is not adjudicable on the shipped showcase sample because the datapack
does not include the original model SQL. Sidq reports all 32 attempts as
`unverifiable`; it does not label them clean or rotten.

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
- The superseded local v2 export uses English narration over silence and contains
  no music; it must not be submitted after the blocking semantics correction.

Use this exact AI/pre-existing-work disclosure in the corresponding Devpost
field:

> AI coding assistants were used to help implement, test, review, and document
> Sidq. Sidq's project code was created during the submission period. The entry
> also includes pre-existing third-party public data and recorded DataHub sample
> metadata, not pre-existing Sidq code; sources, versions, commits, and licences
> are listed in the repository attribution and datasheet files.

## Submission checklist

- [x] Repository link is public and opens without authentication.
- [x] Live project and live DataHub links return HTTP 200 from a logged-out browser.
- [ ] Public video link is viewable without sign-in and is under three minutes.
- [ ] The AI coding-assistant disclosure is pasted exactly and the pre-existing
  third-party data disclosure is included.
- [ ] The Devpost Testing instructions field visible to judges contains working
  `<READER_USERNAME>` / `<READER_PASSWORD>` replacements for a Reader-only
  account; no credential is copied into public fields or committed files.
- [x] The Reader account has been tested to confirm that metadata write controls
  are unavailable.
- [ ] Optional: If pursuing a $50 Most Valuable Feedback bonus prize, submit
  the organizer's feedback form and retain its confirmation; the form is not
  required for project submission.
- [x] Evidence links above open at the submitted repository revision.
- [x] `make gate-demo` and `make check` were run from that revision; paste only
  their actual results into the final submission.
- [x] The 285 findings are described as concentrated in 5 assets after examining
  67 datasets, not as spread across 67 datasets.
- [x] Receipt writeback is described as optional and off by default.

## Owner-only remaining actions

- [ ] Upload the verified V4 English artifact
  `/home/dev/sidq-video/artifacts/video/sidq-final-en.mp4` (SHA-256
  `0811a494c3ee6f78f907c3f2d14908ca18df403d81e38d63093cfa7dab46beef`)
  to public YouTube, Vimeo, or Youku, verify logged-out playback, and replace
  `<PUBLIC_VIDEO_URL>` in the Devpost submission. Do not upload the superseded
  175.317-second v2 MP4s or the rejected black-transition V4 render.
- [ ] Paste the Reader credentials privately into Devpost Testing instructions,
  replace both placeholders there only, and paste the exact disclosure above into
  its corresponding field.
- [ ] Press **Submit**. Optionally complete the organizer's feedback form if
  pursuing its separate bonus.

## Optional opportunities (not submission requirements)

- Join the DataHub Community Slack `#agent-hackathon` channel to ask questions,
  share progress, and connect with other builders.
- To enter the community swag raffle, join `#agent-hackathon`, then either star
  the DataHub repository or post about the hackathon on LinkedIn and tag
  `@DataHub`. Reply to the channel's pinned raffle post with the screenshot or
  link by August 9 at 11:59pm PT.

## Try it

Project link: https://sidq.mlki.app — the page is executable, not a screenshot.
Its **Run it here** buttons execute five demonstrations on the host and print the
real output: the offline verdict reproduction, live MCP catalog audit, dry-run
repair, independent receipt handoff, and measured documentation claims. The
runnable set is a closed table of fixed argument lists with no request input, and
it contains no command that can write to a catalog. `/healthz` proves that the
landing process is alive; `/readyz` separately reports the live DataHub dependency.

Live DataHub: https://datahub.mlki.app. Reader-only judge credentials belong in
the Devpost Testing instructions field above; this public file contains
placeholders only.

Local reproduction:

```bash
make gate-demo
```

That command reproduces the committed fixture verdict. A live local graph is a
separate setup, not implied by the one-command path; follow [`docs/SETUP.md`](SETUP.md)
before running `make live-loop` or the source-drift demonstration.

The repository also includes the worked verdicts, receipt proof, and catalog truth
report under `examples/`.
