# Sidq — with Sidq receipts

> Everyone taught AI to write SQL. We taught it **when to refuse** — and we leave proof.

A DataHub-native gate that runs **before merge** on data-code changes (SQL / dbt / pipelines):
it blocks unsafe changes, proves the verdict with evidence from the metadata graph, and writes
an attested **Sidq receipt** back to the catalog — a receipt that is actually *consumed*
downstream, not decorative write-back.

Circuit breakers stop bad pipeline **runs**. Sidq stops bad pipeline **changes**.

**Status: pre-build.** The idea is locked (the project design contract), the research is complete
(`research/`), the code has not been written yet.

## Submission facts

| | |
|---|---|
| Hackathon | Build with DataHub — The Agent Hackathon (`datahub.devpost.com`) |
| Deadline | **Aug 10, 2026 · 5:00 PM ET** |
| Category | Metadata-Aware Code Generation & Development |
| License | Apache-2.0 (hard requirement — file must sit at repo root) |
| Required component | DataHub + `mcp-server-datahub` (official MCP server) |
| Demo data | `showcase-ecommerce` quickstart datapack (+ healthcare for the PII gate) |

## The gates (all deterministic — zero LLM calls inside gate logic)

1. **Schema gate** — every table/column/type in the change exists in the live graph
2. **Blast-radius gate** — lineage impact: which downstream assets break?
3. **Governance gate** — PII tags, ownership, deprecation, access policy
4. **Quality gate** — existing assertions on touched assets still hold
5. **Receipt** — signed `sidq` receipt written back to DataHub and read back by a later check

Verdict is a policy decision: `block` / `warn` / `pass` — never a prose summary.

## Repo map

| Path | Contents |
|---|---|
| the project design contract | **The contract.** Supersedes every other doc on conflict. |
| `ARCHITECTURE.md` | Flow, packaging surfaces, rubric mapping |

| `research/` | Official rules, resources, MCP/Context-Kit anatomy, strategy, the adversarial verdict that picked this idea |
| `src/sidq/` | Engine + gates (to be written) |
| `examples/` | Passing and blocked cases with their receipts |
| `scripts/` | Smoke tests against the live graph |
| `docs/` | `SETUP.md` and operational notes |

## Ship order (binding cut-line)

1. Schema gate + blast radius on one SQL/dbt PR path
2. GitHub PR-bot with deterministic pass/block comments linking evidence
3. Sidq receipts — written **and** read back, with proof of consumption
4. Governance/PII gate + assertions gate
5. CLI
6. Bounded repair suggestion — built last, dropped first under time pressure

Out of scope, stated honestly in the roadmap: continuous drift sentinel, blanket
quarantine of graph writes, global trust score.

## Disclosures (required by the rules)

- Built during the submission period. Any pre-existing code that gets incorporated is
  disclosed here before submission.
- AI coding assistants were used in development — explicitly permitted by the rules.
