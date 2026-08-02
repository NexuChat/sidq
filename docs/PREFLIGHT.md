# Pre-flight — distilling the gate into the agent's loop

**Status: binding contract, written before any training run.** Written 2026-07-29.
Its point is to fix the failure criteria *in advance*, because the previous
modelling attempt (`docs/LORA.md`) had none and so kept moving its own goalposts.

---

## 0. The problem a model actually solves here

The Sidq engine is an **oracle**: given a diff, the DataHub graph, and the live
database, it produces an exact verdict. It is deterministic, reproducible, and
authoritative.

It is also **slow** — network round-trips to the catalog and the warehouse, on the
order of seconds. An agent drafting SQL cannot call it on every edit. So today the
agent writes freely and is refused at pull-request time, minutes later, after the
reasoning that produced the mistake is gone.

**Pre-flight is a small model that predicts the oracle's verdict from the diff and
cheap local context alone, in under 100 ms, with no network.** It runs inside the
agent's loop. When it is unsure, it says so, and the oracle is called.

## 1. Why a model is legitimate here, when it was not before

Four conditions. A model that fails any one of them is decoration.

| condition | pre-flight | the LoRA attempt |
|---|---|---|
| No deterministic algorithm exists | ✅ without the network the exact verdict is **impossible by definition** | ❌ we compute constraints exactly |
| Labels abundant and free | ✅ **the fixture engine is the labeller** — zero human annotation, suitable for regression consistency but not external accuracy | ❌ prose did not contain the answer; labels were not derivable from inputs |
| Being wrong is cheap | ✅ it is a **pre-filter**; the oracle still decides at the gate | ❌ it was proposed on the verdict path |
| Does something otherwise impossible | ✅ sub-second feedback inside the agent loop | ❌ a probabilistic copy of an exact computation |

## 2. The architectural guarantee

**Pre-flight never touches a verdict.** The project's locked constant — zero LLM
in the verdict — remains literally true. The model is a speed layer above a
deterministic authority that stays the sole decider. Structurally:

```
agent drafting  ──► pre-flight (local, <100ms) ──► "likely BLOCK / unsure / likely PASS"
                                                          │
                          "unsure" or agent asks anyway ───┘
                                                          ▼
pull request    ──────────────────────────────────► ORACLE: sidq check  ──► the verdict
```

The oracle runs at the gate **regardless of what pre-flight said**. A pre-flight
false negative therefore costs latency, never safety.

## 3. Data

Produced by `scripts/generate_mutations.py` + `scripts/label_mutations.py`
(see `docs/BENCHMARK.md`). One corpus, two products: the published miss-rate
benchmark, and this training set.

**The label is the fixture engine's verdict.** The generator's `intent` is never a
label — it exists only so the benchmark can compare intent against verdict. These
self-labels measure whether a local pre-filter reproduces the committed engine and
fixture graph. They are not human annotations and cannot establish real-world
accuracy.

### Input contract — and the leak rule

The model sees only what a local pre-filter could cheaply know:

- the unified diff of the changed SQL
- added / removed / referenced column names
- the touched asset's cached schema
- the cached downstream consumer count

**Nothing derived from the verdict, the evidence, or the rules may enter the
input.** Enforced by an explicit key allow-list asserted in
`tests/test_mutations.py`, not by reviewer vigilance. A leak here would produce a
model that scores beautifully and is worthless — the exact failure mode that is
hardest to notice after the fact.

### Split discipline

Split by **dbt model**, never by row. Mutations of the same model share structure;
a row-wise split lets the model memorise the model rather than learn the rule, and
reports a score that will not survive contact with an unseen repository. Report the
split and the per-split model counts alongside every number.

## 4. The model ladder — earn the complexity

Train **all four rungs** and publish all four. Each must beat the one below it on
the headline metric to justify itself.

| rung | model | why it is here |
|---|---|---|
| L0 | majority class | the number every other rung must beat to mean anything |
| L1 | logistic regression on structured features | if this wins, the "model" is a formula and we ship a formula |
| L2 | gradient-boosted trees on structured features | the honest default for tabular data |
| L3 | small transformer over the raw diff text | **only** if L2's false-negative rate is unacceptable |

Reaching for L3 before L2 has failed would repeat the previous mistake in a new
costume. The deliverable is the cheapest rung that meets the bar, and the published
table showing what the others scored.

## 5. Metrics — one headline, never an average

Three outcomes matter, and they are not interchangeable, so they are never
averaged into an accuracy figure:

- **False negative** — pre-flight says PASS, the oracle says BLOCK. The dangerous
  error. **This is the headline number and it is reported alone.**
- **False positive** — pre-flight says BLOCK, the oracle says PASS. Costs one
  wasted oracle call and some agent friction. Tolerable.
- **Abstention** — pre-flight declines. Costs one oracle call. **Not an error**;
  it is the designed behaviour when the input does not determine the answer.

The operating point is chosen by driving the false-negative rate toward zero and
paying for it in abstentions. A model that abstains on 40% of inputs and never
misses a BLOCK is a success. A model that answers everything at 92% accuracy and
misses one BLOCK in twelve is a failure. Reporting only the second number is how
the previous attempt looked healthier than it was.

## 6. Pre-registered kill criteria

Written before the first training run, and binding.

**Ship pre-flight only if, on the held-out repository split, all three hold:**

1. false-negative rate **≤ 1%**
2. abstention rate **≤ 50%** — beyond that it is a coin flip wearing a model
3. the winning rung beats L0 **and** the rung below it on the same held-out split

**If any fails:** pre-flight is not shipped. `docs/PREFLIGHT.md` gains a "Result:
not shipped" section stating the measured numbers, and the submission ships the
deterministic engine alone — which is complete without it.

A recorded negative result is a real contribution. A model shipped past its own
kill criteria is not.

## 7. Train/serve skew

One feature builder, imported by both the training script and the serving path —
never two implementations that "should" agree. Its source is hashed and the hash is
stored in the model artifact; serving refuses to load a model whose builder hash
does not match. This is the mechanical version of the lesson recorded in
`data/lora/prompt-proof.json`.

## 8. Deliverables

**Result: no model ships, and §1 decided it rather than §6.**

The ladder was trained and evaluated on 2026-07-30 over 20,666 fixture-engine-labelled
mutations, split by dbt model. Two rungs reach perfect held-out regression
consistency — and one
of them is not a model.

`L0.75` is two deterministic pre-checks plus a two-term rule: **block when a
referenced column is missing from the cached schema, or when the change has any
downstream consumer.** Zero missed blocks out of 5,921, zero false alarms, on seven
models never seen in training. The trained rungs match it and never exceed it; they
were imitating it.

That settles §1 before §6 is reached. §1's first condition for a model being
legitimate here is that no deterministic algorithm exists. On this corpus one does,
and it is three lines long — so a model would be decoration, which is the failure
§1 was written to prevent. The deliverable, per §4, is the cheapest rung that meets
the bar, and that is the rule.

**The claim is bounded and the bound matters more than the number.** These fixtures
carry PII tags on one legacy model and almost no ownership or deprecation spread,
so the oracle's verdict here is driven by `unknown_field` and by having any
downstream at all. A graph with real governance variety would not collapse to two
terms. The honest finding is not that pre-flight is easy — it is that **this corpus
cannot tell us whether pre-flight is hard**, and a model trained on it would have
shipped a rule wearing a classifier's clothes.

The full record, including the negative results reached along the way, is
[`docs/PREFLIGHT-RESULTS.md`](PREFLIGHT-RESULTS.md).

| artifact | content | status |
|---|---|---|
| `scripts/eval_preflight.py` | corpus validation against §3 and §6, renders the measured ladder | ✅ built |
| `docs/PREFLIGHT-RESULTS.md` | the kill-criteria verdict — **published whether or not it ships** | ✅ published |
| `scripts/train_preflight.py` | trains the ladder, one seed, deterministic | ✅ built — six rungs trained and published |
| `src/sidq/preflight/` | the feature builder + serving path, only if the criteria are met | ⬜ not built — awaiting the §6 criterion-3 decision |
| MCP tool `preflight_check` | exposed to the agent, only if the criteria are met | ⬜ not built — awaiting the §6 criterion-3 decision |

L3 is not built and that is the spec working. §4 permits a transformer only once L2
has failed *and* earned the escalation; here L2 scores identically to L1 at every
stage, so the trees earn nothing over the formula and the formula is what would
ship. Reaching for L3 anyway is the mistake §4 named in advance.

Writing the criteria before the data existed is what made this readable — and what
made a flawed criterion visible as a flaw rather than as a result. Without them, the
headline would have been L0's 80% self-label consistency, from a model that has learned only that
most changes block. `docs/LORA.md` is the same lesson learned the expensive way.

The deterministic engine ships alone, and §6 pre-committed to exactly that.
