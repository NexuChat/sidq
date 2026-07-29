# Datasheet: dbt documentation claim dataset

## Motivation

This dataset supports extraction of five executable dbt predicate types from schema-documentation sentences: `not_null`, `unique`, `accepted_values`, `relationships`, and `expression`. It is not intended to infer arbitrary business rules.

## Composition

The released dataset has 5000 pairs from 170 repositories: 2500 positive, 1750 negative, and 750 hard-negative. The whole-repository split uses deterministic seed `sidq-dbt-claims-v1` and has 4600 train and 400 eval pairs.

| Split | Positive | Negative | Hard negative | no-claim | not_null | unique | accepted_values | relationships | expression |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Train | 2300 | 1610 | 690 | 2300 | 1455 | 485 | 150 | 114 | 96 |
| Eval | 200 | 140 | 60 | 200 | 40 | 40 | 40 | 40 | 40 |
| Total | 2500 | 1750 | 750 | 2500 | 1495 | 525 | 190 | 154 | 136 |

## Evaluation measurability

A published per-type accuracy requires at least 40 eval examples for that predicate. The no-claim class requires at least 120 eval examples. These are minimum reporting thresholds, not substitutes for uncertainty intervals or error analysis.

| Label | Eval examples | Minimum | Status |
| --- | ---: | ---: | --- |
| not_null | 40 | 40 | measurable |
| unique | 40 | 40 | measurable |
| accepted_values | 40 | 40 | measurable |
| relationships | 40 | 40 | measurable |
| expression | 40 | 40 | measurable |
| no-claim | 200 | 120 | measurable |

The raw crawl contains 68655 rows, 28524 distinct sentences (41.55%), and 30198 distinct (sentence, target) pairs. Exact source/test de-duplication leaves 68622 rows. The global three-occurrence sentence cap leaves 47235 rows, 28524 distinct sentences (60.39%), and 29761 distinct pairs.

After class-balanced selection, the released dataset contains 3806 distinct sentences out of 5000 rows (76.12%) and 4232 distinct (sentence, target) pairs. Every released row records the pre-cap sentence `frequency`.

## Collection process

The collector planned 787 candidates and attempted 740; 303 pinned archives passed the licence gate and were readable enough to record as admitted sources. It pins archives to exact commits, admits only MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, 0BSD, Unlicense, and CC0-1.0, excludes generated `target/` and installed `dbt_packages/`, and preserves repo/path/commit/licence per pair.

Deliberate searches covered `accepted_values`, `relationships`, `dbt_utils.expression_is_true`, `dbt_utils.accepted_range`, and `dbt_expectations.expect_column_values_to_*`. Candidate discovery does not bypass the pinned-archive licence gate.

| Positive predicate | Raw count | Floor | Status |
| --- | ---: | ---: | --- |
| accepted_values | 264 | 150 | met |
| relationships | 329 | 100 | met |
| expression | 175 | 100 | met |

Admitted-source licence counts: Apache-2.0 201, BSD-2-Clause 1, BSD-3-Clause 2, CC0-1.0 1, MIT 96, Unlicense 2.

## Preprocessing

Descriptions are sentence-split. Unsupported, malformed, model-level, or non-literal tests are dropped rather than assigned an invented predicate. Exact source/test duplicates are removed, and identical sentence text is capped globally at three rows with deterministic rare-type-first ordering.

The final class sampler targets 50% positive, 35% negative, and 15% hard-negative. Within each class and positive predicate floor it cycles deterministically across repositories before taking another row from the same repository, preventing a few large packages from dominating. Eval is selected only from held-out repositories and targets 400 pairs, with 40 examples of each predicate type and at least 120 no-claim examples when the pool permits.

| Release-filtering stage | Before | After | Removed | Reason |
| --- | ---: | ---: | ---: | --- |
| Exact source/test de-duplication | 68655 | 68622 | 33 | Repeated identical source/test records |
| Three-occurrence sentence cap | 68622 | 47235 | 21387 | Prevent auto-generated or repeated wording from dominating |
| Balanced, cross-repository sampling | 47235 | 5000 | 42235 | Enforce the release limit, class target, rare-type floors, and whole-repository eval split |

## Uses

Suitable for training and evaluating claim-to-executable-predicate extraction in dbt schema documentation. It is not a complete sample of data-quality rules and should not be used to infer semantics absent from a documented adjacent test.

## Distribution

Records conform to `schema.json`. `ATTRIBUTION.md` lists every pinned, licence-admitted source repository and commit, including sources absent from train/eval after release filtering. `NOTICE` preserves root upstream NOTICE text found in pinned Apache-2.0 archives.

## Maintenance

Use `scripts/mine_dbt_claims.py --resume` after interruption and `--finalize` to regenerate derived files. Re-audit licences and notices before a newly crawled distribution. The Sidq maintainers own updates; no personal or private data removal mechanism is expected because collection is limited to public repository documentation, but upstream removal requests should be reviewed manually.

### Held-out repositories

- dbt-labs/jaffle-shop-classic
- ftupas/dbt-spotify-analytics
- infinitelambda/carbon-analytics
- sfc-gh-dflippo/snowflake-dbt-demo
- ssabenoit/ssa-case-study

### Dropped tests

- unmapped_test:dbt_utils.unique_combination_of_columns: 985
- unmapped_test:dbt_expectations.expect_column_to_exist: 452
- unmapped_expectation:dbt_expectations.expect_column_values_to_be_in_type_list: 401
- model_level_unique_or_not_null: 320
- unmapped_test:dbt_expectations.expect_table_row_count_to_be_between:: 296
- expression_missing_literal: 271
- unmapped_test:dbt_utils.equality: 258
- relationships_missing_to_or_field: 164
- unmapped_test:dbt_utils.relationships_where: 109
- unmapped_test:dbt_expectations.expect_column_value_lengths_to_be_between: 83
- unmapped_test:dbt_expectations.expect_row_values_to_have_recent_data: 82
- unmapped_test:not_empty: 69
- unmapped_test:dbt_expectations.expect_table_row_count_to_be_between: 68
- unmapped_test:is_empty: 55
- unmapped_test:concept_record_completeness: 51
- unmapped_test:dbt_constraints.primary_key: 47
- unmapped_test:dbt_constraints.foreign_key: 37
- unmapped_expectation:dbt_expectations.expect_column_values_to_match_regex: 33
- unmapped_test:dbt_expectations.expect_compound_columns_to_be_unique: 29
- unmapped_test:assert_equal: 25
- unmapped_test:dq_tools.expression_is_true_db: 21
- unmapped_test:dbt_constraints.unique_key: 20
- unmapped_test:dbt_expectations.expect_row_values_to_have_data_for_every_n_datepart: 18
- unmapped_test:person_completeness: 16
- unmapped_test:dbt_expectations.expect_grouped_row_values_to_have_recent_data: 13
- unmapped_test:dbt_expectations.expect_column_value_lengths_to_equal: 12
- unmapped_expectation:dbt_expectations.expect_column_values_to_be_of_type: 10
- unmapped_test:compare_model_subset: 10
- unmapped_test:dbt_expectations.expect_multicolumn_sum_to_equal: 10
- unmapped_test:dbt_expectations.equal_expression: 9
- unmapped_test:dbt_expectations.expect_table_column_count_to_be_between: 9
- unmapped_test:dbt_expectations.expect_table_columns_to_match_ordered_list: 9
- unmapped_test:dbt_expectations.expect_table_row_count_to_equal_other_table_times_factor: 9
- unmapped_test:dbt_utils.equal_rowcount: 9
- unmapped_test:dbt_datamocktool.unit_test: 8
- unmapped_test:is_positive_amount: 8
- unmapped_test:missing_value: 7
- unmapped_test:dbt_expectations.expect_column_distinct_count_to_equal_other_table: 6
- unmapped_test:dbt_expectations.expect_column_pair_values_A_to_be_greater_than_B: 6
- unmapped_test:dbt_expectations.expect_table_aggregation_to_equal_other_table: 6
- unmapped_test:dbt_utils.mutually_exclusive_ranges: 6
- unmapped_test:dbt_utils.not_empty_string: 5
- unmapped_test:dbt_utils.recency: 5
- unmapped_test:value_in_range: 5
- accepted_range_missing_literal_bounds: 4
- unmapped_test:dbt_expectations.expect_table_columns_to_contain_set: 4
- unmapped_test:dbt_expectations.expect_table_columns_to_not_contain_set: 4
- unmapped_test:dbt_expectations.expect_table_row_count_to_equal_other_table: 4
- unmapped_test:dq_tools.equal_rowcount_where_db: 4
- unmapped_test:dq_tools.equality_where_db: 4
- unmapped_test:equality_with_numeric_tolerance: 4
- unmapped_test:is_alphanumeric: 4
- accepted_values_missing_literal_values: 3
- range_expectation_missing_literal_bounds: 3
- unmapped_test:dbt_expectations.expect_column_pair_values_to_be_equal: 3
- unmapped_test:dbt_expectations.expect_column_pair_values_to_be_in_set: 3
- unmapped_test:dbt_expectations.expect_select_column_values_to_be_unique_within_record: 3
- unmapped_test:dbt_expectations.expect_table_column_count_to_equal: 3
- unmapped_test:dbt_expectations.expect_table_column_count_to_equal_other_table: 3
- unmapped_test:dbt_expectations.expect_table_columns_to_match_set: 3
- unmapped_test:dbt_expectations.expect_table_row_count_to_equal: 3
- unmapped_test:dbt_external_tables_integration_tests.tsql_equality: 3
- unmapped_test:null_threshold: 3
- unmapped_expectation:dbt_expectations.expect_column_values_to_be_unique: 2
- unmapped_test:custom_test: 2
- unmapped_test:dbt_assertions.generic_assertions: 2
- unmapped_test:dbt_utils.at_least_one: 2
- unmapped_test:dbt_utils.fewer_rows_than: 2
- unmapped_test:elementary.exposure_schema_validity: 2
- unmapped_test:positive_values: 2
- unmapped_test:re_data.metric_expression_is_true: 2
- unmapped_test:row_count_in_range: 2
- unmapped_test:where_clause: 2
- unmapped_test:advanced_equality: 1
- unmapped_test:boolean_column_matches_condition: 1
- unmapped_test:custom_generic_test: 1
- unmapped_test:date_column_order: 1
- unmapped_test:dbt_datamocktool.unit_test_incremental: 1
- unmapped_test:dbt_semantic_view.materialization_exists: 1
- unmapped_test:dbt_semantic_view.materialization_is_active: 1
- unmapped_test:dbt_utils.is_email: 1
- unmapped_test:dq_tools.recency_db: 1
- unmapped_test:dq_tools.unique_where_db: 1
- unmapped_test:expect_table_columns_to_match_set: 1
- unmapped_test:is_boolean: 1
- unmapped_test:is_not_empty_string: 1
- unmapped_test:is_positive: 1
- unmapped_test:no_duplicate_combination: 1
- unmapped_test:outlier_detection: 1
- unmapped_test:rating_in_range: 1
- unmapped_test:re_data.assert_equal: 1
- unmapped_test:re_data.assert_false: 1
- unmapped_test:re_data.assert_greater_equal: 1
- unmapped_test:re_data.assert_in_range: 1
- unmapped_test:re_data.metric_equal_to: 1
- unmapped_test:re_data.metric_in_range: 1
- unmapped_test:valid_ticket_types: 1

Additionally, 437 repository candidates were skipped because they were unavailable, unreadable, or lacked an unambiguous allowed top-level licence; none contributed records.

### Remaining biases

- Public dbt schema documentation and generic tests overrepresent `not_null` and `unique`; rare-type floors do not make the corpus representative.
- The corpus is biased toward English-speaking analytics projects that publish YAML publicly.
- The lexical hard-negative heuristic does not prove a statement is uncheckable in every organisation.
- Tests adjacent to descriptions are treated as claims expressed by each sentence, which can over-attach predicates when a description contains multiple sentences.
- Repository hold-out reduces house-style leakage, but forks and related package ecosystems can still share wording and conventions.
