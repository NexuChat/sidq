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

Each row names the artifact that settles it, so none of this has to be taken on
trust.

| Criterion | Our answer | Check it |
|---|---|---|
| **Agents that do real work** | `sidq audit` picks its own targets on a catalog it has never seen: it ranks by how much damage a lie would do, spends an explicit budget worst-first, and lets what it finds reorder what it looks at next — a contagious finding promotes the neighbour. `sidq swarm` runs independent workers that divide the same catalog with no coordinator. `sidq repair` proposes only fixes the deterministic engine re-proves against the catalog the fix *would* create, and refuses the rest with reasons. | `make converge-demo` · `make swarm-demo` · `make repair-demo` · [`examples/05`](examples/05-agent-that-stops/) |
| **Depth of DataHub use, incl. write-back** | The entire loop runs on the official `mcp-server-datahub` and nothing else: read with `search`/`get_entities`/`list_schema_fields`/`get_lineage`, decide, write the Receipt through the official mutation tools, then **a separate process reads it back** and recomputes whether it still holds. Sidq also ships its own MCP server with exactly three read-only tools, and a Skill for the DataHub skills ecosystem. | `make live-loop` · [`docs/MCP-CONTRACT.md`](docs/MCP-CONTRACT.md) · [`skills/datahub-verify`](skills/datahub-verify/) |
| **The catalog as shared state** | Receipts written into DataHub let a bounded audit resume where any other instance stopped — no sidecar database, no daemon, no lock. Coverage converges run over run under a budget that never changes. A recorded refusal counts as covered but authorizes nothing; the two are separate fields, not one boolean. | `make converge-demo` · [`docs/RECEIPT-SPEC.md`](docs/RECEIPT-SPEC.md#three-questions-three-fields) |
| **Technical execution** | 1,158 tests at 84.04% branch coverage, including property tests and a hostile-catalog suite that feeds the reader malformed, misdirected, and invented-verdict payloads. The flagship verdict is byte-identical on replay. Published artifacts are generated and gate-checked, so a stale number fails the build rather than reaching a judge. | `make check` · `make gate-demo` · `make regen-check` |
| **Originality** | Six products analyse the *code* in a PR. This asks whether the *catalog* is telling the truth — and proves the question is real on the sponsor's own shipped sample rather than on an example we built to fail. No model participates in a blocking decision; advisory findings can only ever warn, and the boundary is in the output schema. | [`docs/TRUTH-REPORT.md`](docs/TRUTH-REPORT.md) · [`examples/03`](examples/03-catalog-truth-report/) |
| **Real-world usefulness** | A merge gate and an agent guardrail from one engine. The refusal a platform team actually needs is not "this SQL is invalid" but "this change breaks a dashboard owned by another team", and that is the committed example. | [`examples/01`](examples/01-blocked-pii-dashboard/) · sealed PRs [#1](https://github.com/NexuChat/sidq/pull/1)–[#4](https://github.com/NexuChat/sidq/pull/4) |
| **Submission quality** | Four sealed PR threads a judge can read without installing anything, five worked examples, and a claims matrix that gives every public number its scope, its source of truth, and the command that reproduces it. Numbers are asserted against committed evidence by the test suite. | [`docs/CLAIMS-MATRIX.md`](docs/CLAIMS-MATRIX.md) · `tests/test_published_claims.py` |
| **Bonus** | Two upstream pull requests, both open and neither merged at the time of writing. [datahub-project/datahub#19017](https://github.com/datahub-project/datahub/pull/19017) fixes a packaging bug we hit during setup: `datahub.cli.datapack.resources` is absent from `package_data`, so `datahub/cli/datapack/resources/DATAPACK_AGENT_CONTEXT.md` and `datahub/cli/datapack/resources/registry.json` never reach the wheel — `datahub datapack --help` raises `FileNotFoundError` whenever stdout is not a TTY, and the bundled registry fallback that `datahub/cli/datapack/registry.py` advertises cannot fire. [datahub-project/datahub-skills#81](https://github.com/datahub-project/datahub-skills/pull/81) proposes the `datahub-verify` Skill, clean under their prettier and markdownlint configs. A claim-extraction dataset is published Apache-2.0 with attribution, notice, and a datasheet generated from the corpus itself. | [PR #19017](https://github.com/datahub-project/datahub/pull/19017) · [PR #81](https://github.com/datahub-project/datahub-skills/pull/81) · [`skills/datahub-verify`](skills/datahub-verify/) · [`data/claims/DATASHEET.md`](data/claims/DATASHEET.md) |

**What we do not claim.** The Receipt is not a signature, an append-only ledger,
or exactly-once coordination — DataHub stores latest values and offers no
cross-process compare-and-set. `policy_hash` and `commit_sha` make a *fixture*
replay byte-identical; a live decision also depends on the graph it read.
Reproducibility, staleness, and coverage are each scoped precisely in
[`docs/CLAIMS-MATRIX.md`](docs/CLAIMS-MATRIX.md).

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
