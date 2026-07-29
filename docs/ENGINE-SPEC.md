# ENGINE SPEC — wave 1 (binding)

Authority: the project design contract. This file specifies **how** the engine is built; DECISION
specifies **what** must exist. On conflict, DECISION wins.

Non-negotiable: **gates collect evidence, the policy engine decides.** No gate may
return a verdict. No LLM call anywhere in this tree.

---

## 1. Module layout

```
src/sidq/
  models.py            frozen dataclasses (below) — the only shared vocabulary
  resolver.py          git diff  →  TouchedAsset[]
  graph/
    client.py          one wrapper over the DataHub read path (MCP) + write path
    fixtures.py        record/replay so gates are testable without docker
  gates/
    base.py            Gate protocol: collect(change, graph) -> list[Evidence]
    reality.py         Gate 0 — graph schema vs live source schema
    schema.py          Gate 1 — referenced tables/columns exist in the graph
    blast.py           Gate 2 — lineage impact of the change
    governance.py      Gate 3 — PII tags / ownership / deprecation   (wave 5)
    assertions.py      Gate 4 — assertion dependency breakage        (wave 5)
  policy/
    engine.py          Evidence[] + policy.yaml -> Verdict
    default_policy.yaml
  receipt/             build / write / read the sidq receipt         (wave 3)
                       written through the OFFICIAL MCP mutation tools, not a side
                       channel: add_structured_properties (queryable body) +
                       add_tags (sidq:verified | sidq:blocked badge) +
                       save_document (full evidence). Requires
                       TOOLS_IS_MUTATION_ENABLED=true. See `docs/RECON.md`.
  mcp_server/          our own MCP server                            (wave 2)
  bot/                 Verdict -> PR comment markdown                (wave 4)
  cli.py
```

## 2. Data model (`models.py`) — build this first, nothing else compiles without it

```python
@dataclass(frozen=True, slots=True)
class FieldRef:
    dataset_urn: str
    field_path: str

@dataclass(frozen=True, slots=True)
class TouchedAsset:
    urn: str                              # dataset URN in DataHub
    source_path: str                      # repo file that produces it
    added_fields:      tuple[str, ...]
    removed_fields:    tuple[str, ...]
    referenced_fields: tuple[FieldRef, ...]

@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str                             # stable machine id — policy matches on this
    subject: str                          # urn, or "urn#fieldPath"
    detail: dict                          # structured facts, JSON-serializable only
    graph_links: tuple[str, ...] = ()     # deep links into the DataHub UI

@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str                         # "block" | "warn"
    message: str                          # rendered from the rule template
    evidence: tuple[Evidence, ...]

@dataclass(frozen=True, slots=True)
class Verdict:
    decision: str                         # "PASS" | "WARN" | "BLOCK"
    reason_code: str | None               # e.g. "STALE_CONTEXT"
    findings: tuple[Finding, ...]
    touched: tuple[TouchedAsset, ...]
    commit_sha: str
    policy_hash: str                      # sha256 of the policy file actually used
```

**Determinism contract:** identical `(diff, graph snapshot, policy file)` ⇒ byte-identical
verdict JSON. All collections are sorted by a stable key before serialization. The
`policy_hash` makes every attestation reproducible — this is a scoring asset, not a detail.

## 3. Resolver (`resolver.py`) — the component v1 forgot

Input: a git ref range or a list of changed files. Output: `TouchedAsset[]`.

Resolution order, first hit wins, and the chosen strategy is recorded on the asset:
1. **dbt `manifest.json`** — node → `relation_name` → dataset URN. Primary path.
2. **Explicit map** — `.sidq/assets.yml` (`path → urn`), for non-dbt repos.
3. **Naming convention** — configured `platform` + path→schema.table rule.

If a changed file resolves to nothing, that is itself an `Evidence(kind="unresolved_asset")` —
never a silent skip. A gate that cannot see an asset must say so loudly.

Field-level extraction uses **sqlglot** (already a dependency): parse the SQL, walk the
projection and the FROM/JOIN tree, produce `referenced_fields`. Unparseable SQL ⇒
`Evidence(kind="unparseable_sql")`, never a crash.

## 4. Gates

Each gate implements:

```python
class Gate(Protocol):
    id: str
    def collect(self, change: Sequence[TouchedAsset], graph: GraphClient) -> list[Evidence]: ...
```

| Gate | Emits `Evidence.kind` | Notes |
|---|---|---|
| `reality` | `catalog_reality_mismatch` | Compares the graph's schema for each touched dataset against the **live source** (Postgres `information_schema`). `detail` carries `graph_fields`, `live_fields`, `missing_in_graph`, `missing_in_source`. **This is the project's signature — build it in wave 1, not later.** |
| `schema` | `unknown_field`, `unknown_dataset`, `type_mismatch` | Referenced tables/columns exist in the graph, types compatible. |
| `blast` | `blast_radius` | Downstream impact per touched asset, via `get_lineage` **and `get_lineage_paths_between`** — record the *path*, not just a count; the path is the evidence a judge wants rendered. `detail`: `downstream_count`, `downstream_urns`, `paths`, `dashboards`, `critical_assets`, `cross_team_owners`, `depth`, `granularity` (`"column"` or `"table"` — set from RECON; if column-level lineage is absent in the sample, degrade to table-level and record that honestly). |

Gates never raise on graph errors: a failed lookup becomes `Evidence(kind="graph_unavailable")`,
which the default policy treats as **block** (fail closed — a gate that fails open is not a gate).

**Verified MCP call contract (2026-07-28 — `docs/MCP-CONTRACT.md`, real server, not docs).**
Several signatures we had guessed were wrong and are now corrected: `search` rejects
`entity_type`; `get_entities` takes `urns` (plural), not `urn`; `list_schema_fields` takes
`urn`, not `dataset_urn`. For the blast gate specifically:

- **Column-level lineage is switched on by passing `column: "<fieldPath>"`** alongside the
  ordinary lineage arguments. `get_lineage_paths_between` takes `source_column` /
  `target_column`.
- `lineageColumns` sits on each `downstreams.searchResults[]` item — **not** inside its
  `entity`.
- `metadata.queryType == "column-level-lineage"` is the **reliable** signal for the
  `granularity` field. Derive it from that, never from whether a column happened to be passed.

## 5. Policy engine (`policy/engine.py`) — the product

`policy.yaml` is a **restricted matcher, never `eval`**:

```yaml
version: 1
settings:
  wide_blast_radius_threshold: 5
rules:
  - id: catalog_reality_mismatch
    match: { evidence_kind: catalog_reality_mismatch }
    severity: block
    reason_code: STALE_CONTEXT
    message: >-
      The catalog disagrees with the live source for {subject}.
      Graph: {detail.graph_fields}. Source: {detail.live_fields}.
      Any agent reading DataHub right now would build on a lie.

  - id: critical_downstream
    match:
      evidence_kind: blast_radius
      where:
        - { field: detail.critical_assets, op: not_empty }
    severity: block

  - id: wide_blast_radius
    match:
      evidence_kind: blast_radius
      where:
        - { field: detail.downstream_count, op: gt, value: "$settings.wide_blast_radius_threshold" }
    severity: warn
```

Supported ops only: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `empty`, `not_empty`.
Unknown op or unknown field ⇒ hard config error at load time, not at decision time.

Resolution: any `block` ⇒ `BLOCK`; else any `warn` ⇒ `WARN`; else `PASS`.
`reason_code` comes from the highest-severity rule that declares one.
Evidence with **no matching rule** is retained in the verdict as informational — visible,
never silently dropped.

## 6. CLI (`cli.py`) — wave 1 exit criterion

```
sidq check --diff HEAD~1..HEAD [--policy path] [--json]
sidq check --file model.sql
sidq explain <rule_id>
```

Exit codes: `0` PASS, `1` WARN, `2` BLOCK. Human output is a table; `--json` is the
canonical machine artifact that every other surface (MCP, bot, receipt) consumes.

## 7. Tests — "green must be honest"

- **Fixture-backed gate tests**: record real graph responses once into `graph/fixtures.py`,
  replay in pytest. No docker required in CI.
- **Policy engine tests**: pure, table-driven — evidence in, verdict out.
- **Resolver tests**: a tiny dbt manifest fixture + a naming-convention case + an
  unresolvable file (must produce `unresolved_asset`, must not crash).
- **Determinism test**: run the same input twice, assert byte-identical JSON.
- **Golden end-to-end**: implemented in `tests/test_golden_examples.py` against the
  real published example, `examples/01-blocked-pii-dashboard`, rather than the
  `bad_change.sql` / `good_change.sql` fixtures named in the first draft of this
  spec — those were never created, so for a time this regression did not exist and
  the engine could have changed its verdict on the artifact a judge opens. The test
  runs offline from the committed replay snapshot and pins the BLOCK decision, the
  offline-provable rule ids, the column-level lineage under them, and byte
  determinism. Its scope limits and one known drift are recorded in
  the project gap assessment §5 items 25–26. These are regression tests — never edit an
  example to make a test pass; fix the engine.

## 8. Wave 1 definition of done

`pytest -q` green · `sidq check` produces a stable verdict JSON on the live
quickstart graph · Gate 0 demonstrably fires after renaming a column in the live
Postgres while the catalog still shows the old name.
