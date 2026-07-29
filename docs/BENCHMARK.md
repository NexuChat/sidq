# Mutation Benchmark

## Full run

This is the complete deterministic corpus generated with:

```sh
.venv/bin/python scripts/generate_mutations.py --seed 0 --out data/benchmark/mutations.jsonl
.venv/bin/python scripts/label_mutations.py --in data/benchmark/mutations.jsonl --out data/benchmark/labelled.jsonl --jobs 8
```

The run labelled **20,666 / 20,666** generated mutations across 20 model files. Labeling used eight workers and took approximately **1 minute 38 seconds** wall-clock (started 2026-07-29 18:51:30 UTC; output completed at 18:53:08 UTC).

Every generated record has exactly one labelled record, all IDs are unique, and there are **0 `engine_error` rows**. No records were silently dropped.

## Fixture coverage and honest interpretation

The recorded graph fixtures cover the legacy `models/order_entry/customers.sql` baseline and its lineage. The 18 newly added model files are not represented in that graph. Consequently, a mutation without sufficient graph coverage is fail-closed as `BLOCK(UNVERIFIABLE_CHANGE)` (normally with the `graph_unavailable` rule). This means the change was *not verified*; it must not be counted as a rule-based catch.

The buckets below are deliberately mutually exclusive:

- **Caught by analysis**: a harmful mutation that was blocked with a reason other than `UNVERIFIABLE_CHANGE`; a concrete rule fired.
- **Blocked as unverifiable**: a `BLOCK` with reason `UNVERIFIABLE_CHANGE`, regardless of generator intent.
- **MISSED**: a harmful mutation with verdict `PASS`.

The 13 analysis catches were all on the covered legacy model:

| Family | Catches | Specific rules fired |
|---|---:|---|
| `drop_selected_column` | 1 | `pii_exposure`, `wide_blast_radius` |
| `rename_selected_column` | 6 | `pii_exposure`, `wide_blast_radius` |
| `reference_nonexistent_upstream_column` | 6 | `pii_exposure`, `unknown_field`, `wide_blast_radius` |

## Three-bucket summary

| Outcome | Harmful | Benign | Total |
|---|---:|---:|---:|
| Caught by analysis | 13 | 0 | 13 |
| Blocked as unverifiable | 11,853 | 8,800 | 20,653 |
| MISSED (harmful -> PASS) | 0 | — | 0 |
| Other verdicts / engine errors | 0 | 0 | 0 |
| **Total** | **11,866** | **8,800** | **20,666** |

All 20,666 labels have verdict `BLOCK`; 20,653 are `UNVERIFIABLE_CHANGE` and only 13 are concrete analysis blocks. Reporting all `BLOCK`s as catches would falsely inflate the catch count from 13 to 20,666.

## Generator family histogram and outcome breakdown

| Family | Intent | Total | Caught by analysis | Blocked as unverifiable | MISSED |
|---|---|---:|---:|---:|---:|
| `add_non_pii_derived_column` | benign | 2,000 | 0 | 2,000 | 0 |
| `add_or_remove_sql_comment` | benign | 2,000 | 0 | 2,000 | 0 |
| `change_aggregation_grain` | harmful | 300 | 0 | 300 | 0 |
| `change_column_type_cast` | harmful | 2,000 | 0 | 2,000 | 0 |
| `change_join_key` | harmful | 466 | 0 | 466 | 0 |
| `delete_where_filter` | harmful | 100 | 0 | 100 | 0 |
| `drop_selected_column` | harmful | 2,000 | 1 | 1,999 | 0 |
| `expose_pii_tagged_column` | harmful | 1,000 | 0 | 1,000 | 0 |
| `reference_nonexistent_upstream_column` | harmful | 2,000 | 6 | 1,994 | 0 |
| `reformat_whitespace` | benign | 2,000 | 0 | 2,000 | 0 |
| `rename_cte` | benign | 300 | 0 | 300 | 0 |
| `rename_local_alias` | benign | 500 | 0 | 500 | 0 |
| `rename_selected_column` | harmful | 2,000 | 6 | 1,994 | 0 |
| `reorder_select_list` | benign | 2,000 | 0 | 2,000 | 0 |
| `replace_explicit_select_with_star` | harmful | 2,000 | 0 | 2,000 | 0 |
| **Total** | — | **20,666** | **13** | **20,653** | **0** |

## MISSES

**Count: 0.** There are no harmful-intent `PASS` verdicts in `data/benchmark/labelled.jsonl`, so there are no miss diffs to list.

## False alarms

False alarms are benign-intent `BLOCK`s **excluding** the `UNVERIFIABLE_CHANGE` bucket. **Count: 0.** The 8,800 benign `BLOCK`s are all fail-closed unverifiable changes, not analysis false alarms; therefore there are no false-alarm diffs to list.

## Verification and reproduction

- `wc -l data/benchmark/mutations.jsonl data/benchmark/labelled.jsonl` returns **20,666** for each file.
- The published tables are computed solely from `data/benchmark/mutations.jsonl` and `data/benchmark/labelled.jsonl`; the latter contains verdicts, reasons, rules, evidence, context, and any engine error.
- Spot-check: recomputing with a one-line Python query from `labelled.jsonl` returned **20,653** `UNVERIFIABLE_CHANGE` labels and **13** non-unverifiable concrete blocks, matching the summary and table totals.
- The full test suite passed after the run: **295 passed, 2 skipped**.

This benchmark measures a deterministic generator against the fixture-backed engine. It is a useful regression measure, not a claim of real-world coverage; in particular, fixture absence is intentionally reported as unverifiable rather than as detection.
