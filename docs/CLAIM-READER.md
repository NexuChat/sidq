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

The embedding model is loaded from the pinned revision
`31de22b673913c7d658c0f03f792d77c2dcf8ebd`, not a moving `main` tag. Every run
also exposes the committed head fingerprint and its confidence threshold. A
warning can therefore be attributed to the exact reader that proposed its SQL;
upgrading either half is an explicit, reviewable artifact change rather than
silent model drift.

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

## Rules or a model — measured, on the same rows

`docs/DECISION-COST.md` shows a three-line rule beating a classifier by three
orders of magnitude. Showing that comparison and not this one would be
selective, so both are scored by the same code path in the same run.

| reading a documented sentence | proposals | precision | recall |
| --- | ---: | ---: | ---: |
| the deterministic reader | 23 | 17.4% | 3.4% |
| the trained reader | 72 | **95.8%** | **58.0%** |

The rules are not badly written; they are being asked for something regular
expressions do not do. Their nineteen mistakes divide into three kinds, and one
of them matters much more than the others:

- **Ten drop a qualifier.** "Alternative titles must be unique **within a
  step**" is read as globally unique. That is the dangerous failure — it tests a
  stronger claim than the documentation made, and the disagreement it would
  report is one nobody wrote down. The trained reader made this mistake zero
  times.
- **Seven are a convention, not an error.** "Primary key." is read as `unique`
  where the corpus labels it `not_null`. A primary key is both.
- **Two fell for a hard negative**, which is what hard negatives are for.

The trained reader's four mistakes are all in the other direction: sentences the
corpus labels as claiming nothing, where it proposed uniqueness — "Unique
identifier of the template". Over-reading a genuinely uniqueness-shaped sentence
is the cheaper way to be wrong, and the boundary makes it cheaper still.

**So neither candidate is stronger; each is stronger at one task, and the two
tasks are the two sides of the boundary.**

| | deciding a verdict | reading a sentence |
| --- | --- | --- |
| accuracy | identical — the rule ties the classifier | not close — 95.8% against 17.4% |
| speed | rule wins by ~2,700× | rule is faster and it does not matter |
| what ships | **the rule** | **the model** |

Speed decides only where accuracy ties. On verdicts it ties, so the nanoseconds
settle it. On prose it does not tie, so they are irrelevant: a reader that is
twenty-five microseconds faster and covers a seventeenth as much is not the
cheaper option, it is the one that does not do the job.

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
It can change advisory `WARN` coverage, and that is why its revision and head
fingerprint are reported. It can never grant permission and can never produce a
`BLOCK`; the blocking decision remains entirely deterministic.

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
