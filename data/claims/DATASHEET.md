# Datasheet: merged constraint-pair corpus (v7)

> **Generated from the released corpus.** Every number in *Composition*,
> *Per-source released accounting*, and *Type coverage* is recomputed from
> `train.jsonl` and `eval.jsonl` by `scripts/datasheet_stats.py`.
> `scripts/datasheet_stats.py --check` runs in the regeneration gate, so the
> corpus and this document cannot drift apart. Do not edit those sections by
> hand.

## Composition

This release has **2,576 rows** (2,048 train / 528 eval) drawn from 459 distinct source documents. Its class mix is 49.88% positive / 35.09% negative / 15.02% hard-negative; the global identical-sentence cap is three.

Distinct sentences: **2,371 / 2,576 (92.04%)**. The distinct count is the meaningful corpus size, not the raw row count.

## Per-source released accounting

Released rows only — what this repository ships and anyone can count. The
mining funnel that produced them is described under *Mining provenance*,
and is not reproducible from this release.

| Source | Released rows | Documents | no_claim | unique | not_null | accepted_values | expression | relationships | Distinct sentences / rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| raw-v3 / dbt + SQL DDL | 1,794 | 193 | 1,291 | 403 | 97 | 2 | 0 | 1 | 1,674 / 1,794 (93.31%) |
| raw-v4 / FHIR R5 | 464 | 133 | 0 | 0 | 0 | 0 | 294 | 170 | 398 / 464 (85.78%) |
| schema-corpora / SchemaStore | 287 | 104 | 0 | 14 | 94 | 74 | 51 | 54 | 269 / 287 (93.73%) |
| raw-v5 / application-code choices | 23 | 23 | 0 | 0 | 0 | 23 | 0 | 0 | 22 / 23 (95.65%) |
| raw-v6 / error-message & dictionary lane | 8 | 6 | 0 | 0 | 1 | 0 | 7 | 0 | 8 / 8 (100.00%) |

## Type coverage

**`accepted_values` is the smallest labelled class at 99 pairs**, and `unique` the largest at 417. Every positive row carries exactly one constraint type; `no_claim` rows are the negatives and hard-negatives.

| Type | Released positive pairs | Working floor (1,500) | Status | Gap |
| --- | ---: | ---: | --- | ---: |
| unique | 417 | 1,500 | under-sampled | 1,083 |
| not_null | 192 | 1,500 | under-sampled | 1,308 |
| accepted_values | 99 | 1,500 | under-sampled | 1,401 |
| expression | 352 | 1,500 | under-sampled | 1,148 |
| relationships | 225 | 1,500 | under-sampled | 1,275 |

No constraint type reaches the working floor. These are counted rows, not
padded targets, and the gap column is the honest distance to a corpus that
would support a per-type claim.

Lane composition per type:

- `unique` — 403 from raw-v3 / dbt + SQL DDL, 14 from schema-corpora / SchemaStore.
- `not_null` — 97 from raw-v3 / dbt + SQL DDL, 94 from schema-corpora / SchemaStore, 1 from raw-v6 / error-message & dictionary lane.
- `accepted_values` — 74 from schema-corpora / SchemaStore, 23 from raw-v5 / application-code choices, 2 from raw-v3 / dbt + SQL DDL.
- `expression` — 294 from raw-v4 / FHIR R5, 51 from schema-corpora / SchemaStore, 7 from raw-v6 / error-message & dictionary lane.
- `relationships` — 170 from raw-v4 / FHIR R5, 54 from schema-corpora / SchemaStore, 1 from raw-v3 / dbt + SQL DDL.

## Filtering, balancing, and split

Every positive was rechecked with its native-sentence predicate:
dbt/application-code/error-message pairs use the corrected `expressed()`
predicate, SchemaStore uses `expresses_constraint()`, and FHIR uses its
assertion/reference predicates. `accepted_values` labels are reduced to the
enum subset literally stated by the sentence, and numeric expressions
recognise only unambiguous semantic bounds. A sentence that merely names a
field or reports an undifferentiated failure is rejected. No description,
title, adjacent code, or synthetic prose was used to rescue a pair.

Candidates were read in raw-source order, deduplicated on `(sentence, column,
target)`, and retain the first occurrence's `source_kind`. The cap follows
that deduplication. The split is a deterministic full-document/repository
holdout: `hash(source_kind, source_document) mod 5 == 4` is eval.

That holdout is checked, not asserted: **0 source documents appear in both splits**, and eval carries 528 rows covering 5 of 5 constraint types (`accepted_values`, `expression`, `not_null`, `relationships`, `unique`).

The source data contain negatives and hard-negatives only in the dbt lane.
Cross-corpus negative sampling is therefore not possible without inventing
labels; negatives are instead sampled round-robin across held-out
repository/document lanes, so no individual dbt repository dominates.
Positive sources are all retained.

## Mining provenance — historical, not reproducible from this release

The collection pools that these rows were mined from are not shipped in
full. `data/claims/raw/` contains the dbt-lane pool only; the SchemaStore,
FHIR, and licence-gated GitHub-clone pools are not part of this repository.
The funnel figures recorded during collection — rows scanned, positive
candidates, per-source survival rates — are therefore **historical results
of a one-time mining run, and cannot be re-derived from this release**. They
are deliberately absent from the generated tables above rather than restated
as if a reader could check them.

What can be re-derived is everything the released files contain, which is
what the sections above report.

## Licence and mining result

Included sources are dbt repositories (per-row permissive licence),
SchemaStore at its pinned Apache-2.0 commit, FHIR R5 core 5.0.0 under
CC0-1.0, and the licence-gated GitHub clone records (MIT, Apache-2.0, or BSD
as retained per row). Google Discovery remains excluded because payload
redistribution terms were not confirmed. THO, NIEM, and SchemaPile remain
excluded on their recorded copyright/licence grounds. AWS Smithy and Data
Contract CLI supplied no accepted row.

The error-message and dictionary lane did not substantiate the expected high-yield result: it contributes 8 released rows, and its repository-discovery records are not training examples.
