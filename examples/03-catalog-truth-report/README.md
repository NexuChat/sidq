# 03 — the truth report on DataHub's own sample data

We ran Sidq's catalog self-contradiction audit against `showcase-ecommerce` — the
demo dataset DataHub itself ships — and published what it found. Not a catalog we
built to fail. Theirs.

```bash
.venv/bin/python examples/03-catalog-truth-report/generate_report.py
```

The committed result is [`report.json`](report.json); the full finding-by-finding
write-up is [`docs/TRUTH-REPORT.md`](../../docs/TRUTH-REPORT.md).

## What it examined, and what it found

Read-only catalog metadata only. No source code, no database, no external system
was consulted — which is what makes the headline finding hard to argue with: it
is a contradiction **inside the catalog**, between two things the catalog itself
asserts.

| Check                          | Datasets | Assets | Findings | Unverifiable |
| ------------------------------ | -------: | -----: | -------: | -----------: |
| `lineage_field_missing`        |       67 |     67 |  **285** |            0 |
| `unowned_consumed`             |       67 |     82 |   **29** |            0 |
| `pii_leak_untagged`            |       67 |     67 |        0 |            0 |
| `doc_references_missing_column`|       67 |     67 |        0 |            0 |
| `orphan_lineage`               |       67 |     82 |        0 |            0 |
| `deprecated_upstream_of_live`  |       67 |     82 |        0 |            0 |

Scope: all 67 datasets, 12 charts, and 3 dashboards — 82 catalog entities — over
938 in-scope lineage and membership edges.

**The 285 findings are one proposition, applied 285 times.** Stored lineage claims
that a column feeds a target field, and that target's own stored schema does not
list the field. Both claims are in the catalog. They cannot both be true. They
concentrate in 5 target assets, which is what you would expect from a
contradiction with a cause rather than 285 unrelated accidents.

The 29 `unowned_consumed` findings are a different kind: assets something
downstream depends on, with nobody recorded as responsible for them.

## What it did *not* find, stated as plainly

`lineage_rot` — the check that compares catalog lineage against the model SQL
that would produce it — **returned no adjudicable finding on this catalog, and
that is not a clean bill.** 32 of the 67 datasets carry fine-grained lineage, and
all 32 attempts came back `lineage_unverifiable`: the sample pack does not ship
the original dbt model SQL, so there is nothing to compare against.

Zero findings and zero checkable are different results, and a report that
presented the first while meaning the second would be committing the error this
whole project is about. The four zero-finding rows above *were* adjudicated. The
`lineage_rot` row is absent because it could not be.

The two local SQL files under `demo/` and `examples/01-blocked-pii-dashboard/`
were deliberately excluded from that count. They are Sidq's own demonstration
edits, not sample-pack source, and using them would have manufactured a finding.

## Why this artifact exists

Every entrant can say the problem is real. This is the version a judge can check:
run the command, read `report.json`, and compare it against a catalog they can
install themselves in one command. The numbers here are pinned by
`tests/test_published_claims.py` against `report.json`, so the README, the
document, and the evidence cannot drift apart.
