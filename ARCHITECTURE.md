# Sidq — the refusal capability for the agent era

> The shape of the system. Build details live in `docs/ENGINE-SPEC.md`.

Everyone taught AI to write SQL. We taught it **when to refuse** — and we leave inspectable evidence.

**Hackathon:** Build with DataHub — The Agent Hackathon (deadline Aug 10, 2026 · 5PM ET)
**License:** Apache-2.0 at repo root (hard requirement)
**Required component:** DataHub + `mcp-server-datahub` — Sidq's graph dependency —
and `sidq-mcp`, the three-tool server we ship.

## The gap

DataHub MCP already does context-aware SQL generation; Vanna, Wren AI and dbt Copilot all
generate. **Sidq's contribution is a deterministic, DataHub-native pre-merge refusal path**
that combines catalog evidence, explicit policy, and an optional queryable Receipt a later
agent can independently re-check.

![The Sidq loop: change → gates → policy → optional receipt write → shared current state](docs/architecture.svg)

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
[3]  GOVERNANCE GATE ownership and deprecation evidence
      │              (PII tags remain sensitivity context; no route-delta claim)
[4]  SELF-CHECK      graph claim ⟷ graph claim: schema, lineage, and governance
      │
      ▼  gates emit EVIDENCE only — never verdicts
[P]  POLICY ENGINE   policy.yaml → PASS / WARN / BLOCK, per named rule, with evidence links
      │
[S]  SIDQ RECEIPT    optional, operator-enabled write of current, queryable values
```

## Three delivery surfaces, one engine

1. **MCP server (ours)** — exactly three agent tools: `check_change(diff|sql)`,
   `verify_context(urn)`, and `search_verified(query)`. A coding agent asks permission
   *before* proposing data code; an analytics agent verifies context before trusting it;
   another agent can select assets carrying fresh records in the Sidq MCP verification
   store. That store is not DataHub Receipt state. Independent Receipt consumption is the
   separate `sidq verify` path, which reads DataHub again and checks current graph context,
   policy, and age.
2. **CLI** — the full operator surface: `sidq check`, `audit`, `repair`, `swarm`,
   `claims`, and `verify`. Every command returns deterministic human output or canonical
   JSON from the same evidence and policy engine.
3. **GitHub PR bot** — a deterministic comment: verdict, the rule that fired, blast radius,
   receipt link, and exact reproduction command. Judges can picture installing it tomorrow.

The official `mcp-server-datahub` is not a fourth Sidq surface. It is the graph
dependency used to read DataHub and perform explicit receipt mutations;
`sidq-mcp` is the agent-facing Sidq server above.

## The agents, and the shared current state they can re-check

`sidq audit` spends an explicit budget worst-first and names what it did not
reach. `sidq repair` proposes only what the engine re-proves, and refuses the
rest with reasons. When an operator explicitly enables the optional Receipt write,
DataHub stores the latest receipt values, not append-only history. `sidq audit
--resume` reads that shared current state back, skips every asset a current
receipt already covers under the current policy hash — including one recording a
refusal, which is reported as `BLOCKED` and authorizes nothing — and spends the
whole budget on assets no run has seen. Coverage converges under a fixed budget, and any
Sidq instance with access to the same catalog can re-check the latest values before
choosing work. No sidecar database or daemon is required for this optimization.

## Rubric mapping

| Criterion | Our answer |
|---|---|
| Depth of DataHub use **incl. write-back** | reads schema + lineage + governance; explicitly writes a queryable receipt that a *different* agent reads back with `sidq verify`; the agent-facing MCP remains exactly the three documented verification tools |
| Technical execution | deterministic end-to-end on the judges' own quickstart; fixture-backed tests; byte-identical verdicts for identical inputs |
| Originality | inverts the category — verification-first, source-agnostic; Gate 0 catches the catalog lying about live reality at PR time, with no daemon; optional current Receipt values let bounded audits resume from shared current state |
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
| Datafold / Recce | the closest real competitor — column-level impact in PRs. We differ: OSS and DataHub-native, governance/PII policy rather than data-diff, an explicit queryable receipt in the catalog, and an MCP surface for agents |

## Stack (boring on purpose)

DataHub OSS quickstart via docker · `mcp-server-datahub` (official) · Python 3.12 +
sqlglot + the MCP client · **zero LLM calls inside gate logic** — the signature constraint.

## Disclosures (rules require)

Built during the submission period. Any incorporated pre-existing snippet is disclosed in
the README. AI coding assistants were used — explicitly permitted by the rules.
