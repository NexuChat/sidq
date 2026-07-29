# Model extractor bake-off

The bake-off has not been run in this checkout yet: Ollama is not running locally. Run `ollama serve`, pull the three candidate models, and then execute `.venv/bin/python scripts/bakeoff.py`. The runner will replace this file with the measured two-round report and stores resumable checkpoints under `data/bakeoff/`.

Round 1 uses 16 examples per predicate type plus 40 no-claim examples (120 total). Five types × 24 plus 40 would be 160, so this is the consistent 120-call interpretation of the requested screening budget.

The report separates positive exact/type-only extraction accuracy from the headline safety metric: negative abstention (the share of no-claim examples on which an extractor returns `None`).
