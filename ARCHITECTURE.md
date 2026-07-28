# sidq — The Verification Gate for AI‑Generated Data Code

> Everyone taught AI to write SQL. We taught it when to refuse — and how to repair.

> Circuit breakers stop bad pipeline **runs**. sidq stops bad pipeline
> **changes** — before they merge. (Explicit differentiation from the existing
> runtime circuit-breaker pattern in the DataHub/Airflow ecosystem.)

**Hackathon:** Build with DataHub — The Agent Hackathon (deadline Aug 10, 2026 · 5PM ET)
**Category:** Metadata‑Aware Code Generation & Development
**License:** Apache‑2.0 (hackathon requirement — NOT MIT)

## The gap (verified by market research 2026‑07‑23)
DataHub MCP already does context‑aware SQL generation; Vanna 2.0 / Wren AI / dbt
Copilot all generate. **Nobody gates.** No tool in the landscape blocks failing
generated code from being proposed, and none writes verification receipts back
to the metadata graph. sidq is the missing half of the category.

## What it does (one flow)
```
proposed data code (SQL / dbt model / pipeline)  ← from ANY source:
      │                                            human, Copilot, Vanna, any agent…
      ▼
[1] SCHEMA GATE      — every table/column/type exists in the live graph (via DataHub MCP)
[2] BLAST‑RADIUS GATE — lineage impact: which downstream assets/dashboards break?
                        (turns DataHub's manual impact view into an automated CI gate)
[3] GOVERNANCE GATE  — PII tags, ownership, deprecation, access policies enforced
[4] QUALITY GATE     — existing assertions/tests on touched assets still hold
      │
      ├─ any gate fails → [4.5] HEAL — propose the SMALLEST verified fix from the
      │                   exact diagnostics (bounded, ≤2 attempts, re-gated fully);
      │                   still failing → verdict: BLOCKED + diagnostics
      │                   (never "proposed anyway")
      ▼ all pass (original or healed)
[5] RECEIPT          — verification receipt written BACK to DataHub as metadata
                        on the asset/PR (judging criterion: "contribute back to the graph")
      ▼
PR‑ready output + human‑readable verdict
```

## Packaging — three surfaces, one engine
1. **GitHub Action / PR‑bot** (the hero surface): a real PR receives an automatic
   comment — verdict, blast‑radius graph, receipt link. Judges can imagine
   installing it tomorrow; this is the "real-world usefulness" knockout.
2. **CLI** — `sidq check file.sql` for local/CI use.
3. **Verdict panel** — tiny dark editorial web view (house style).

## Why it wins on the judging rubric
| Criterion | Our answer |
|---|---|
| Use of DataHub | reads schemas+lineage+governance+assertions AND writes receipts back |
| Technical execution | deterministic gates end‑to‑end, demo on real quickstart data |
| Originality | inverts the category: verification-first + verified-repair, source-agnostic; explicitly complementary to runtime circuit breakers (pre-merge vs post-deploy) |
| Real‑world usefulness | it's a CI gate — every data platform team's daily pain |
| Submission quality | proven playbook (Build Week pipeline: video/cards/README) |
| Bonus | upstream contribution: a DataHub Skill or docs PR for the gate pattern |

## Stack (boring on purpose)
- **DataHub quickstart** via docker compose (local, self‑contained demo data)
- **mcp-server-datahub** (official) — the required component ✓
- Agent: Python 3.12 + the official MCP client; LLM only for intent/explanation,
  ZERO model calls inside the gates (deterministic — our signature)
- CLI + tiny web verdict panel (dark editorial style, Kufi — house style)
- `examples/` folder: passing + blocked cases with receipts (rules recommend it)

## Honest disclosures (rules require)
- Built during the submission period; incorporates the author's pre‑existing
  orchestration experience; any reused snippets disclosed in README.
- AI coding assistants used (allowed explicitly by the rules).

## Timeline
- Jul 24–25: env up (quickstart + MCP), gate 1 walking skeleton
- Jul 26–31: gates 2–4 + receipts write‑back
- Aug 1–4: verdict panel + examples/ + hardening
- Aug 5–8: README, video (<3 min), description
- **Aug 9: submit (one full safety day)**
