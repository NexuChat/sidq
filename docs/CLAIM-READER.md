# The documentation reader — measured, and bounded

> Trained by `scripts/train_claim_reader.py`. The head and its evaluation report
> are committed under `data/claims/reader/`, and `tests/test_published_claims.py`
> asserts that the numbers below still match the committed report.

`docs/PREFLIGHT.md` §1 says a model is legitimate here only where no
deterministic algorithm exists. That rule is what kept a model out of the
verdict, and it is the same rule that puts one here. Deciding whether a change
is safe has an algorithm — `docs/PREFLIGHT-RESULTS.md` found it, and
`docs/DECISION-COST.md` measures what it costs. Reading a sentence a person
wrote in a catalog description does not.

So the boundary has two sides, and `sidq claims` is where both are visible:

**a model may decide what to test · only the engine may decide what is true**

## What the reader is

A linear head over [`microsoft/harrier-oss-v1-270m`][harrier], a multilingual
embedding model covering 94+ languages. The input is one field description plus
the column and table it belongs to; the output is one of six labels — the five
claim types, or none.

Multilingual is not a bonus feature here. A catalog's descriptions are written
in whatever language its team speaks, and `tests/fixtures/catalog_corpus.py`
already exercises eight. A regular expression reads one of them.

## What it was measured at

Trained on 2,048 rows, evaluated on a held-out 528 from `data/claims/`.

| head | accuracy | precision | recall | proposals |
| --- | ---: | ---: | ---: | ---: |
| logistic regression | 89.2% | **95.8%** | **58.0%** | 72 |
| gradient-boosted trees | 87.7% | 96.2% | 42.0% | 52 |

The linear head ships. The boosted one was ahead on precision by 0.4 points
across fifty-odd proposals, which is noise, while giving up sixteen points of
recall and adding a training stack to inference — so the same rule
`PREFLIGHT-RESULTS.md` §4 applies to the pre-flight ladder applies here: among
candidates that clear the bar, take the simplest.

**Precision is the number that was tuned for, not accuracy.** A wrongly-typed
proposal compiles into a query that tests something the documentation never
said, and a finding from that query would be a fabricated one. The operating
threshold is the lowest that clears 95% precision on held-out data.

## What it will not propose

Only `unique` and `not_null` — the two claim types fully specified by their type
alone. `accepted_values` needs the value list, `relationships` and `expression`
need an expression, and a classifier does not produce arguments. Those labels
are still learned and still measured; they are simply never acted on, because a
half-specified claim would compile into a query testing something nobody wrote.

## Why a wrong reading is survivable

Three independent properties, each enforced in code and pinned by a test in
`tests/test_attest.py`:

1. **The deterministic reader runs first.** The model is consulted only on
   sentences the rules declined, so it can never overturn a deterministic
   reading — only reach sentences no deterministic reading covers.
2. **An untested model claim contributes nothing.** Not a warning, not a
   lower-confidence finding — nothing. If the query could not run, what remains
   is the model's reading of a sentence, and a reading is not a measurement. A
   *rule*-proposed claim survives the same failure, because its reading is
   deterministic and re-derivable by anyone.
3. **A model-proposed claim can never reach a `BLOCK`.** The untestable ones are
   dropped before the engine sees them, and a violated documentation claim is a
   warning by policy — a documented sentence disagreeing with the data is worth
   a person's attention, not an automatic refusal.

So the worst a wrong reading can do is ask someone to look at a real column.
The model can be replaced, upgraded, or removed tomorrow and no verdict moves.

## The generative attempt, and why it is not what shipped

A local generative model was tried first: give it the sentence, ask for
conforming JSON. On the held-out positives it proposed nothing at all — the
task is classification, and asking a sub-billion-parameter model to emit
structured output is not the shape of this problem.

That attempt did surface a real defect in this repository, now fixed. Reasoning
models were being detected by exact tag, so `qwen3.5:0.8b` fell through a set
containing `qwen3.5:4b` and `qwen3.5:2b`, spent its whole token budget thinking,
and returned an empty completion on every sentence — which the extractor read as
an abstention. A model producing nothing looked exactly like a model exercising
judgment. Families are now matched by prefix, and an empty completion raises
rather than returning `None`: silence is not a decision.

## Reproducing it

```bash
# on a machine with a GPU, once — caches embeddings for both splits
scripts/train_claim_reader.py --embed

# anywhere — trains both heads, picks the operating point, writes the artifacts
scripts/train_claim_reader.py --fit --precision 0.95
```

Then, against a live catalog and its source:

```bash
sidq claims <dataset-urn> --source "<read-only postgres dsn>" --reader
```

[harrier]: https://huggingface.co/microsoft/harrier-oss-v1-270m
