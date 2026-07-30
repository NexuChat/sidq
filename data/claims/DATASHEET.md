# Datasheet: merged constraint-pair corpus (v7)

## Honest composition

This release has **2,576 rows** (2,048 train / 528 eval). Its class mix is 50.00% positive / 35.01% negative / 14.99% hard-negative; the global identical-sentence cap is three.

Distinct sentences: **2,377 / 2,582 (92.06%)**. The distinct count is the meaningful corpus size, not the raw row count.

## Per-source × per-type accounting

Survival is evaluated on each source's raw positive candidates before cross-source pair deduplication; final counts are after pair deduplication, the three-sentence cap, document holdout, and class balancing. SchemaStore's denominator is described constrained nodes (not emitted pair rows). `raw-v6` physically contains 6,360 rows, of which 6,338 are repository-discovery/skip metadata with no sentence or target and therefore cannot be candidates.

| Source | Input rows | Positive candidates | Positive survived | Survival | Released rows | no_claim | unique | not_null | accepted_values | expression | relationships | Distinct sentences / rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| raw-v3 / dbt + SQL DDL | 77729 | 5384 | 935 | 17.37% | 1794 | 1291 | 403 | 97 | 2 | 0 | 1 | 1674 / 1794 (93.31%) |
| raw-v3 / SchemaStore | 746 emitted / 29,683 described nodes | 29683 | 746 pairs / 743 nodes | 2.50% (nodes) | 293 | 0 | 14 | 94 | 79 | 52 | 54 | 275 / 293 (93.86%) |
| raw-v4 / FHIR R5 | 642 | 642 | 642 | 100.00% | 464 | 0 | 0 | 0 | 0 | 294 | 170 | 398 / 464 (85.78%) |
| raw-v5 / application-code choices | 33 | 33 | 28 | 84.85% | 23 | 0 | 0 | 0 | 23 | 0 | 0 | 22 / 23 (95.65%) |
| raw-v6 / error-message & dictionary lane | 6,360 physical / 22 extractable | 22 | 9 | 40.91% | 8 | 0 | 0 | 1 | 0 | 7 | 0 | 8 / 8 (100.00%) |

## Type coverage

**accepted_values: 104 final pairs** (23 from application-code choices, 79 from SchemaStore, and 2 from dbt). The error-message lane added none under the unchanged filter.

| Type | Final positive pairs | Working floor (1,500) | Status | Gap |
| --- | ---: | ---: | --- | ---: |
| unique | 417 | 1,500 | under-sampled | 1,083 |
| not_null | 192 | 1,500 | under-sampled | 1,308 |
| accepted_values | 104 | 1,500 | under-sampled | 1,396 |
| expression | 353 | 1,500 | under-sampled | 1,147 |
| relationships | 225 | 1,500 | under-sampled | 1,275 |

No constraint type reaches the existing 1,500-pair working floor. Unique is closest (417); accepted_values remains the smallest class (99). These are real counts, not padded targets.

## Filtering, balancing, and split

Every positive was rechecked with its native-sentence predicate: dbt/application-code/error-message pairs use the corrected `expressed()` predicate, SchemaStore uses `expresses_constraint()`, and FHIR uses its assertion/reference predicates. `accepted_values` labels are reduced to the enum subset literally stated by the sentence, and numeric expressions recognise only unambiguous semantic bounds. A sentence that merely names a field or reports an undifferentiated failure is rejected. No description, title, adjacent code, or synthetic prose was used to rescue a pair.

Candidates were read in raw-source order (raw-v3, SchemaStore, raw-v4, raw-v5, raw-v6), deduplicated on `(sentence, column, target)`, and retain the first occurrence's `source_kind`. The cap follows that deduplication. The split is a deterministic full-document/repository holdout: `hash(source_kind, source_document) mod 5 == 4` is eval. Thus no repository or source document is in both files. Eval has 528 rows and includes all five constraint types.

The source data contain negatives and hard-negatives only in raw-v3. Therefore cross-corpus negative sampling is not possible without inventing labels; negatives are instead sampled round-robin across held-out repository/document lanes, so no individual dbt repository dominates. Positive sources are all retained.

## Raw-pool accounting

Admitted extractable candidates: 79,172; 6,338 raw-v6 metadata-only rows were excluded before this count. Detailed pre-release counts: 79,172 extractable rows, 31,760 distinct pairs after filtering and pair deduplication, 30,942 rows after the sentence cap, and 2,576 released rows. The exact before/after filter counts are recorded in `refilter-v6-report.json`.

## Licence and mining result

Included sources are dbt repositories (per-row permissive licence), SchemaStore at its pinned Apache-2.0 commit, FHIR R5 core 5.0.0 under CC0-1.0, and the licence-gated GitHub clone records from raw-v5/raw-v6 (MIT, Apache-2.0, or BSD as retained per row). Google Discovery remains excluded because payload redistribution terms were not confirmed. THO, NIEM, and SchemaPile remain excluded on their recorded copyright/licence grounds. AWS Smithy and Data Contract CLI supplied no accepted row.

The v6 run did not substantiate the expected high-yield result: it mined 22 extractable pairs from 124 inspected candidates, and only 9 pass the unchanged release predicate before deduplication. Its 6,338 metadata-only discovery rows are not training examples.
