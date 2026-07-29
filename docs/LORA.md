# Qwen3.5-0.8B LoRA evaluation

## Method

All three arms used the same 400 repository-held-out examples in `data/claims/eval.jsonl`: 40 positives for each of five predicate types and 200 no-claim examples. A and B ran sequentially in one CPU-only `transformers` + `peft` runtime. Both use the byte-identical `ModelExtractor._prompt` formatter proven in `data/lora/prompt-proof.json`, deterministic decoding, a JSON-Schema grammar constraint, and the production strict JSON-to-claim validator. A disables the loaded PEFT adapter; B enables that exact adapter. C is `RuleBasedExtractor` and makes no model call. Progress checkpoints every 20 examples in `data/bakeoff/<arm>/progress.jsonl` and is resumed by index.

Exact match requires type, column, and required values/expr. Type-only ignores predicate arguments. Positive accuracy is never averaged with negative abstention.

## Per-predicate results

| Predicate type (n=40) | A: prompted base exact / type-only | B: LoRA exact / type-only | B − A exact | C: rule-based exact / type-only |
| --- | ---: | ---: | ---: | ---: |
| not_null | 0.0% / 0.0% | 45.0% / 45.0% | +45.0 pp | 0.0% / 0.0% |
| unique | 0.0% / 0.0% | 52.5% / 52.5% | +52.5 pp | 0.0% / 0.0% |
| accepted_values | 0.0% / 0.0% | 2.5% / 30.0% | +2.5 pp | 0.0% / 0.0% |
| relationships | 0.0% / 0.0% | 55.0% / 62.5% | +55.0 pp | 0.0% / 0.0% |
| expression | 0.0% / 0.0% | 0.0% / 0.0% | +0.0 pp | 0.0% / 0.0% |

## Negative abstention — headline safety measure

| Arm | Correct `None` / 200 negatives | Negative abstention | Hallucinated claims | Median latency / call |
| --- | ---: | ---: | ---: | ---: |
| A: prompted base | 200 / 200 | 100.0% | 0 | 2.419s |
| B: LoRA | 160 / 200 | 80.0% | 40 | 1.994s |
| C: rule-based | 200 / 200 | 100.0% | 0 | 0.000s |

## Verdict

1. **Did the adapter beat the prompted base?** Yes on macro positive exact match: 0.0% → 31.0% (+31.0 pp). Per type: not_null +45.0 pp; unique +52.5 pp; accepted_values +2.5 pp; relationships +55.0 pp; expression +0.0 pp.

2. **Did it improve abstention, or make the model more eager to invent claims?** It did not improve abstention: 100.0% → 80.0% (-20.0 pp). It is equally or more eager to invent claims on no-claim prose; this is the single most important result.

3. **Does the rule-based baseline beat either model arm on any type?** No predicate type has a strict rule-based exact-match win over both model arms.

4. **Is the adapter worth shipping at all?** No: it has not earned an additional artifact because it failed to improve both positive utility and negative abstention.

## Known limitation

The adapter currently requires `transformers` + `peft`: llama.cpp cannot yet convert this `qwen3next` architecture correctly, so the adapter is not a drop-in Ollama model today. The shipped default is the rule-based extractor and requires no model at all; this limits only the optional model upgrade, not the product.
