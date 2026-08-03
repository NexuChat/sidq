# Dataset v2: labels derivable from text

v2 exists because the first release trained and evaluated a relationship that was not present in its input: a dbt description was labelled from an adjacent test. The evaluation review found that the prompted base model correctly returned `null` almost always, while the LoRA learned to guess from column names; its negative abstention fell from 99.0% to 81.0% and it produced 38 hallucinated claims. See [LORA.md](LORA.md#root-cause--why-every-arm-looks-the-way-it-does-added-after-review).

The v2 deterministic claim-expression filter retained 909 of 6844 prior positives (13.28%). It filters only: every retained label is still the literal dbt test mapped by the existing miner; no model generated or changed a label.

A bare categorical parenthetical such as `Type of game (regular/playoff)` is rejected. It does not state that other values are invalid. Numeric parenthetical ranges such as `(0-100)` count when they match the test bounds because both bounds are explicitly stated.

The previous adjacency-labelled files (`data/claims/train-v1.jsonl` and `data/claims/eval-v1.jsonl`) are not part of the public release; the v2 files are the only dataset shipped. v2 keeps permissive-licence provenance, a repository-level hold-out, the identical-sentence cap of three, recorded seed, and both negative classes.

## Measurement consequence

The honest filter makes some types too small for a 40-example per-type evaluation. The current v2 eval counts are: `not_null` 22, `unique` 175, `accepted_values` 1, `relationships` 0, `expression` 2.

Not measurable at this sample size: `not_null`, `accepted_values`, `relationships`, `expression`. These shortages are reported rather than padded with labels that the sentence does not entail.
