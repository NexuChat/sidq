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
| Labels abundant and free | ✅ **the oracle is the labeller** — zero human annotation | ❌ prose did not contain the answer; labels were not derivable from inputs |
| Being wrong is cheap | ✅ it is a **pre-filter**; the oracle still decides at the gate | ❌ it was proposed on the verdict path |
| Does something otherwise impossible | ✅ sub-second feedback inside the agent loop | ❌ a probabilistic copy of an exact computation |

## 2. The architectural guarantee

**Pre-flight never touches a verdict.** the project design contract's locked constant — zero LLM
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

**The label is the oracle's verdict.** The generator's `intent` is never a label —
it exists only so the benchmark can compare intent against verdict.

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

| artifact | content |
|---|---|
| `scripts/train_preflight.py` | trains all four rungs, one seed, deterministic |
| `scripts/eval_preflight.py` | held-out evaluation, emits the metric table |
| `docs/PREFLIGHT-RESULTS.md` | the four-rung table, headline false-negative rate, chosen operating point, and the kill-criteria verdict — **published whether or not it ships** |
| `src/sidq/preflight/` | the feature builder + serving path, only if the criteria are met |
| MCP tool `preflight_check` | exposed to the agent, only if the criteria are met |
