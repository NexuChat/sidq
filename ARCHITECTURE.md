# Sidq — the refusal capability for the agent era

> The shape of the system. Build details live in `docs/ENGINE-SPEC.md`.

Everyone taught AI to write SQL. We taught it **when to refuse** — and we leave proof.

**Hackathon:** Build with DataHub — The Agent Hackathon (deadline Aug 10, 2026 · 5PM ET)
**License:** Apache-2.0 at repo root (hard requirement)
**Required component:** DataHub + `mcp-server-datahub` — and we ship an MCP server of our own.

## The gap

DataHub MCP already does context-aware SQL generation; Vanna, Wren AI and dbt Copilot all
generate. **Nobody gates.** No tool blocks a failing generated change before it merges, and
none writes a verification receipt back to the metadata graph that a later agent reads.

## The flow

```
git diff (SQL / dbt)
      │
[R]  RESOLVER        changed file → dataset URN + touched fields
      │              (dbt manifest → explicit map → naming convention)
      ▼
[0]  REALITY GATE    graph schema  ⟷  live source schema
      │              disagree? → STALE_CONTEXT: "the catalog itself is lying"
[1]  SCHEMA GATE     referenced tables/columns/types exist in the graph
[2]  BLAST GATE      lineage impact: which downstream assets and dashboards break?
[3]  GOVERNANCE GATE PII tags, ownership, deprecation, access policy
[4]  ASSERTION GATE  does the change remove a field an existing assertion depends on?
      │
      ▼  gates emit EVIDENCE only — never verdicts
[P]  POLICY ENGINE   policy.yaml → PASS / WARN / BLOCK, per named rule, with evidence links
      │
[S]  SIDQ RECEIPT    attested, queryable, written back onto the affected assets
```

## Three surfaces, one engine

1. **MCP server (ours)** — `check_change(diff|sql)` and `get_verification_status(urn)`.
   Any coding agent asks permission *before* proposing data code; any analytics agent
   checks an asset's last verdict *before* querying it. This is what makes the receipt
   consumed by a third party rather than by ourselves.
2. **CLI** — `sidq check --diff HEAD~1..HEAD`. The engine's interface; every other
   surface consumes its JSON.
3. **GitHub PR bot** — a deterministic comment: verdict, the rule that fired, blast radius,
   receipt link. Judges can picture installing it tomorrow.

## Rubric mapping

| Criterion | Our answer |
|---|---|
| Depth of DataHub use **incl. write-back** | reads schema + lineage + governance + assertions; writes an attested receipt that a *different* agent reads back through our MCP tool |
| Technical execution | deterministic end-to-end on the judges' own quickstart; fixture-backed tests; byte-identical verdicts for identical inputs |
| Originality | inverts the category — verification-first, source-agnostic; and Gate 0 catches the catalog lying about live reality at PR time, with no daemon |
| Real-world usefulness | a CI gate plus an agent guardrail — the daily pain of every data platform team |
| Submission quality | public demo repo with real sealed PRs judges can read without installing anything |
| Bonus | upstream `datahub-verify` skill |

## Why we are not our neighbours

| Neighbour | Why we are not it |
|---|---|
| dbt tests / SQL linters | syntax or runtime; zero knowledge of the graph |
| Airflow circuit breakers | stop bad **runs** after deploy; we stop bad **changes** before merge |
| Monte Carlo / Bigeye | post-hoc observability for humans, as a separate product; we are a decision inside the PR, written into the graph |
| DataHub Metadata Tests | Cloud-only, and its rules are metadata-internal; we are OSS and we compare against **live reality** |
| Datafold / Recce | the closest real competitor — column-level impact in PRs. We differ: OSS and DataHub-native, governance/PII policy rather than data-diff, an attested receipt in the catalog, and an MCP surface for agents |

## Stack (boring on purpose)

DataHub OSS quickstart via docker · `mcp-server-datahub` (official) · Python 3.12 +
sqlglot + the MCP client · **zero LLM calls inside gate logic** — the signature constraint.

## Disclosures (rules require)

Built during the submission period. Any incorporated pre-existing snippet is disclosed in
the README. AI coding assistants were used — explicitly permitted by the rules.
