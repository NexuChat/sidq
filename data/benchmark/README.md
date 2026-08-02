# Regression benchmark artifacts

`labelled-regression.jsonl.gz` is the complete 20,666-row fixture-engine output
used by the clean-clone published-claim guards. It is gzip-compressed because the
canonical JSONL is 32 MB but highly repetitive; decompression produces SHA-256
`f57e1aba52db21f4d15e5236ff527551763deac358870b4cb0cd22bae71d51ea`.
The gzip writer fixes its timestamp and filename fields, so packaging the same
sorted records is byte-reproducible.

The rows are self-labelled by Sidq's fixture engine. Generator `intent` is retained
for comparison, but it is not human truth and these artifacts do not establish
real-world accuracy. They measure regression consistency against the committed
fixture graph. `preflight-rungs.json` contains the deterministic model-disjoint
split result derived from the same local corpus.

Regenerate the local intermediates and committed regression evidence with:

```bash
.venv/bin/python scripts/generate_mutations.py --out data/benchmark/mutations.jsonl
.venv/bin/python scripts/label_mutations.py \
  --in data/benchmark/mutations.jsonl \
  --out data/benchmark/labelled.jsonl \
  --report docs/BENCHMARK.md \
  --regression-artifact data/benchmark/labelled-regression.jsonl.gz
.venv/bin/python scripts/train_preflight.py
.venv/bin/python scripts/eval_preflight.py
```

The two uncompressed JSONL files remain ignored build intermediates. The compact
regression artifact and rung summary are committed so CI does not skip evidence
guards in a clean clone.
