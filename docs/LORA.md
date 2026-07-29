# Qwen3.5-0.8B LoRA evaluation

## Method

All three arms used the same 400 repository-held-out examples in `data/claims/eval.jsonl`: 40 positives for each of five predicate types and 200 no-claim examples. A and B ran sequentially in one CPU-only `transformers` + `peft` runtime. Both use the byte-identical `ModelExtractor._prompt` formatter proven in `data/lora/prompt-proof.json`, deterministic decoding, a JSON-Schema grammar constraint, and the production strict JSON-to-claim validator. A disables the loaded PEFT adapter; B enables that exact adapter. C is `RuleBasedExtractor` and makes no model call. Progress checkpoints every 20 examples in `data/bakeoff/<arm>/progress.jsonl` and is resumed by index.

Exact match requires type, column, and required values/expr. Type-only ignores predicate arguments. Positive accuracy is never averaged with negative abstention.

## Per-predicate results

| Predicate type (n=40) | A: prompted base exact / type-only | B: LoRA exact / type-only | B − A exact | C: rule-based exact / type-only |
| --- | ---: | ---: | ---: | ---: |
| not_null | 0.0% / 0.0% | 47.5% / 47.5% | +47.5 pp | 0.0% / 0.0% |
| unique | 0.0% / 0.0% | 45.0% / 45.0% | +45.0 pp | 0.0% / 0.0% |
| accepted_values | 0.0% / 0.0% | 2.5% / 27.5% | +2.5 pp | 0.0% / 0.0% |
| relationships | 0.0% / 0.0% | 52.5% / 62.5% | +52.5 pp | 0.0% / 0.0% |
| expression | 0.0% / 0.0% | 0.0% / 0.0% | +0.0 pp | 0.0% / 0.0% |

## Negative abstention — headline safety measure

| Arm | Correct `None` / 200 negatives | Negative abstention | Hallucinated claims | Median latency / call |
| --- | ---: | ---: | ---: | ---: |
| A: prompted base | 198 / 200 | 99.0% | 2 | 0.206s |
| B: LoRA | 162 / 200 | 81.0% | 38 | 0.195s |
| C: rule-based | 200 / 200 | 100.0% | 0 | 0.000s |

## Verdict

1. **Did the adapter beat the prompted base?** Yes on macro positive exact match: 0.0% → 29.5% (+29.5 pp). Per type: not_null +47.5 pp; unique +45.0 pp; accepted_values +2.5 pp; relationships +52.5 pp; expression +0.0 pp.

2. **Did it improve abstention, or make the model more eager to invent claims?** It did not improve abstention: 99.0% → 81.0% (-18.0 pp). It is equally or more eager to invent claims on no-claim prose; this is the single most important result.

3. **Does the rule-based baseline beat either model arm on any type?** No predicate type has a strict rule-based exact-match win over both model arms.

4. **Is the adapter worth shipping at all?** No: it has not earned an additional artifact because it failed to improve both positive utility and negative abstention.

## Known limitation

The adapter currently requires `transformers` + `peft`: llama.cpp cannot yet convert this `qwen3next` architecture correctly, so the adapter is not a drop-in Ollama model today. The shipped default is the rule-based extractor and requires no model at all; this limits only the optional model upgrade, not the product.

---

## Root cause — why every arm looks the way it does (added after review)

The numbers above are correct, but the verdict "the adapter failed" is not the
interesting part. Independent inspection of the eval positives explains all
three arms at once, and indicts the **dataset**, not the training.

Sample of labelled positives, with the label mined from the adjacent dbt test:

| sentence | mined label |
|---|---|
| `Business-friendly order status description` | `not_null` |
| `Player's first name` | `not_null` |
| `Sum of final prices including tax` | `expression` |
| `Type of game (regular/playoff)` | `accepted_values` |

**None of these sentences states a checkable claim.** A dbt author writes
`description: "Player's first name"` and separately adds `tests: [not_null]`
because *they* know the column is required. The constraint lives in the author's
head, not in the prose.

The mining step assumed *description + adjacent test = (prose → predicate) pair*.
That assumption is wrong. Adjacency is not expression.

### This explains every row

- **Arm A returning `null` almost always is the correct answer**, not a failure.
  Its 99.0% abstention is not a virtue either — it never extracts anything, so
  abstention and inability are indistinguishable here.
- **Arm B learned to guess from the column name, not the sentence.** That is why
  it reaches 47.5% on `not_null` and 45.0% on `unique` — both guessable from
  naming convention (`*_id` → unique) — and **0.0% on `expression`**, which
  requires actual semantic content.
- **Abstention collapsing from 99.0% to 81.0% is the direct consequence.** On an
  unlearnable mapping, always guessing maximises training accuracy. We taught the
  model to guess, so it guesses — including on the 200 sentences that contain no
  claim at all.
- **The rule-based extractor scoring 0.0% is it being right.** Verified
  independently: it returns `None` on all 200 positives because there is nothing
  in those sentences to extract. Its 100% abstention and zero hallucinations are
  the honest behaviour on this data.

### What this changes

1. **The adapter is not shipped.** Confirmed, but for a better-understood reason:
   it was trained on labels that cannot be derived from its input.
2. **The dataset needs a filter, not more rows.** Keep only pairs where the text
   *expresses* the constraint — "one row per customer", "status is active or
   inactive", "never null" — and discard descriptions that merely happen to sit
   beside a test. That is a much smaller, much harder, and much more useful set.
3. **The product is unaffected.** The shipped default is the rule-based
   extractor, which behaves correctly here: it stays silent when there is nothing
   to extract. That is the property the whole project is built on.

### The general lesson

We built a benchmark whose labels were not present in its inputs, then measured a
model against it and got a confident, precise, meaningless table. The measurement
was only useful because we checked whether a *zero* made sense — and it did.

This is the same failure this project exists to detect: a stored claim that no
one verified against the thing it describes.

---

## v2 — retrain on filtered, expressed-constraint data

The filter changed the problem rather than the model: it retained only pairs
where the sentence itself expresses the constraint. The filtered corpus has
1,209 rows (809 train / 400 held-out eval), down from 5,000 v1 rows. Its 605
positives are 513 `unique`, 82 `not_null`, 5 `accepted_values`, 1
`relationships`, and 4 `expression`; the remaining 604 rows are negatives.

`data/lora/adapter-v1/` remains the preserved v1 artifact. The new v2 adapter
is `data/lora/adapter/`; it was trained from the deliberately unchanged
`Qwen/Qwen3.5-0.8B` base with r=16, alpha=32, attention+MLP targets, one epoch,
and learning rate 2e-4. Training used the shared GPU at the requested 16% cap
(3.13 GiB peak reserved). `data/lora/prompt-proof.json` was refreshed and proves
the training prompt is byte-identical to `ModelExtractor._prompt`.

### Method

All three arms used the same 400 repository-held-out v2 examples in
`data/lora/v2/eval.jsonl`. A and B ran sequentially in one GPU
`transformers` + `peft` runtime using deterministic JSON-schema-constrained
decoding and the production strict JSON-to-claim validator; the only difference
is that A disables the loaded PEFT adapter and B enables it. C is
`RuleBasedExtractor`. Evaluation used a tighter 12% CUDA allocation cap to
leave room for the KYC service, which is within the required maximum of 16%.
Results and resumable records are retained in `data/lora/v2/`.

Exact match requires type, column, and required values/expr. Type-only ignores
predicate arguments. Positive accuracy is never averaged with negative
abstention.

### Per-predicate results

| Predicate type (eval n) | A: prompted base exact / type-only | B: v2 LoRA exact / type-only | B − A exact | C: rule-based exact / type-only |
| --- | ---: | ---: | ---: | ---: |
| not_null (n=22) | 0.0% / 0.0% | 0.0% / 0.0% | +0.0 pp | 0.0% / 0.0% |
| unique (n=175) | 0.0% / 0.0% | 100.0% / 100.0% | +100.0 pp | 8.0% / 8.0% |
| accepted_values (n=1) | not measurable at this sample size | not measurable at this sample size | not measurable | not measurable at this sample size |
| relationships (n=0) | not measurable at this sample size | not measurable at this sample size | not measurable | not measurable at this sample size |
| expression (n=2) | not measurable at this sample size | not measurable at this sample size | not measurable | not measurable at this sample size |

The filtered corpus contains only 5 `accepted_values`, 1 `relationships`, and
4 `expression` positives in total; their held-out counts are 1, 0, and 2.
They are explicitly not measurable at this sample size, so no accuracy is
reported for them.

### Negative abstention — headline safety measure

| Arm | Correct `None` / 200 negatives | Negative abstention | Hallucinated claims | Median latency / call |
| --- | ---: | ---: | ---: | ---: |
| A: prompted base | 199 / 200 | 99.5% | 1 | 0.208s |
| B: v2 LoRA | 178 / 200 | 89.0% | 22 | 0.506s |
| C: rule-based | 200 / 200 | 100.0% | 0 | 0.000s |

### Verdict

1. **Does v2 beat the 8% rule-based baseline on `unique`?** Yes: the v2
   adapter reaches 100.0% exact/type-only on 175 held-out `unique` examples,
   versus 8.0% for rules. This confirms that filtering supplied real semantic
   signal for the only adequately sampled predicate.
2. **Did abstention hold?** **No.** It falls from 99.5% for the prompted base
   to 89.0% for the adapter (-10.5 pp), creating 22 hallucinated claims. This
   is better than v1's 81.0% abstention but still below the 95.0% safety bar.
3. **Ship decision:** **do not ship the v2 adapter; ship rules only.** The v2
   data fix solved `unique` extraction but did not solve the safety failure.
   The model has therefore not earned an additional production artifact.
