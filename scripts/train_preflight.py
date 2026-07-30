#!/usr/bin/env python3
"""Train and evaluate the pre-flight ladder from `docs/PREFLIGHT.md` §4.

Four rungs, all trained, all published, each required to beat the one below it:

    L0  majority class            the number every other rung must beat
    L1  logistic regression       if this wins, the "model" is a formula
    L2  gradient-boosted trees    the honest default for tabular data
    L3  transformer over the diff  only if L2's false-negative rate is unacceptable

L3 is deliberately not implemented. §4 says reaching for it before L2 has failed
"would repeat the previous mistake in a new costume", so it is built only if the
published L2 numbers justify it.

Two rules from the spec are enforced in code rather than trusted:

- **§3 leak rule.** Features are built only from the diff, the column names, and
  the cached downstream count. Nothing derived from the verdict, the evidence, or
  the rules is reachable, because the feature builder is handed the context dict
  alone and never the record.
- **§3 split discipline.** The split is by dbt model, never by row. Mutations of
  one model share structure, so a row-wise split measures memorisation and reports
  a number that will not survive an unseen repository.

The headline is the false-negative rate — pre-flight says PASS, the oracle says
BLOCK — reported alone, never averaged into an accuracy figure (§5).

Usage:
    scripts/train_preflight.py
    scripts/train_preflight.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CORPUS = ROOT / "data" / "benchmark" / "labelled.jsonl"
RESULTS = ROOT / "data" / "benchmark" / "preflight-rungs.json"
SEED = 0
# §5: the operating point is chosen by driving false negatives toward zero and
# paying for it in abstentions. A prediction is only emitted when the model is at
# least this confident; everything else abstains and costs one oracle call.
CONFIDENCE_FLOOR = 0.90


def load_rows() -> list[dict]:
    with CORPUS.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def features(context: dict) -> dict[str, float]:
    """Build the §3 input vector from the change context alone.

    This function is given the context and never the record, so a verdict-derived
    feature is not merely forbidden — it is unreachable.
    """
    added = context.get("added_fields") or []
    removed = context.get("removed_fields") or []
    referenced = context.get("referenced_fields") or []
    files = context.get("changed_files") or []
    urns = context.get("touched_urns") or []
    text = " ".join(str(name).lower() for name in [*added, *removed, *referenced])
    return {
        "added": float(len(added)),
        "removed": float(len(removed)),
        "referenced": float(len(referenced)),
        "files": float(len(files)),
        "resolved_urns": float(len(urns)),
        "unresolved": 0.0 if urns else 1.0,
        "downstream": float(context.get("downstream_count") or 0),
        "has_downstream": 1.0 if (context.get("downstream_count") or 0) else 0.0,
        "mentions_email": 1.0 if "email" in text else 0.0,
        "mentions_id": 1.0 if "_id" in text else 0.0,
        "is_staging": 1.0 if any("staging/" in str(f) for f in files) else 0.0,
        "is_mart": 1.0 if any("marts/" in str(f) for f in files) else 0.0,
        "is_intermediate": (
            1.0 if any("intermediate/" in str(f) for f in files) else 0.0
        ),
    }


def split_by_model(rows: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Hold out whole dbt models, per §3. Deterministic, no shuffling."""
    models = sorted(
        {str((row["context"].get("changed_files") or ["?"])[0]) for row in rows}
    )
    holdout = {model for index, model in enumerate(models) if index % 3 == 0}
    train = [
        row
        for row in rows
        if str((row["context"].get("changed_files") or ["?"])[0]) not in holdout
    ]
    test = [
        row
        for row in rows
        if str((row["context"].get("changed_files") or ["?"])[0]) in holdout
    ]
    return train, test, sorted(holdout)


def _score(name: str, truth: list[int], predicted: list[int | None]) -> dict:
    """§5: three outcomes, never averaged. 1 means BLOCK, 0 means PASS."""
    answered = [(t, p) for t, p in zip(truth, predicted, strict=True) if p is not None]
    abstained = len(predicted) - len(answered)
    # The dangerous error: pre-flight said PASS, the oracle said BLOCK.
    false_negatives = sum(1 for t, p in answered if t == 1 and p == 0)
    false_positives = sum(1 for t, p in answered if t == 0 and p == 1)
    blocks = sum(1 for value in truth if value == 1)
    return {
        "rung": name,
        "evaluated": len(truth),
        "answered": len(answered),
        "abstained": abstained,
        "abstention_rate": abstained / len(truth) if truth else 0.0,
        "false_negatives": false_negatives,
        # Denominated in real BLOCKs, not in all rows: the question is what share
        # of genuinely blocking changes this would have waved through.
        "false_negative_rate": false_negatives / blocks if blocks else 0.0,
        "false_positives": false_positives,
        "accuracy": (
            sum(1 for t, p in answered if t == p) / len(answered) if answered else 0.0
        ),
    }


def run() -> dict:
    import numpy
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    rows = load_rows()
    train, test, holdout = split_by_model(rows)
    keys = sorted(features(rows[0]["context"]))

    def matrix(subset: list[dict]) -> tuple:
        x = numpy.array(
            [[features(row["context"])[key] for key in keys] for row in subset],
            dtype=float,
        )
        y = numpy.array(
            [1 if row["verdict"] == "BLOCK" else 0 for row in subset], dtype=int
        )
        return x, y

    train_x, train_y = matrix(train)
    test_x, test_y = matrix(test)
    truth = [int(value) for value in test_y]

    rungs = []

    # L0 — majority class. Answers everything, never abstains.
    majority = round(float(train_y.mean()))
    rungs.append(_score("L0 majority class", truth, [majority] * len(truth)))

    def with_floor(probabilities) -> list[int | None]:
        return [
            1 if p >= CONFIDENCE_FLOOR else 0 if p <= 1 - CONFIDENCE_FLOOR else None
            for p in probabilities
        ]

    scaler = StandardScaler().fit(train_x)
    l1 = LogisticRegression(max_iter=2000, random_state=SEED).fit(
        scaler.transform(train_x), train_y
    )
    rungs.append(
        _score(
            "L1 logistic regression",
            truth,
            with_floor(l1.predict_proba(scaler.transform(test_x))[:, 1]),
        )
    )

    l2 = HistGradientBoostingClassifier(random_state=SEED).fit(train_x, train_y)
    rungs.append(
        _score(
            "L2 gradient-boosted trees",
            truth,
            with_floor(l2.predict_proba(test_x)[:, 1]),
        )
    )

    return {
        "seed": SEED,
        "confidence_floor": CONFIDENCE_FLOOR,
        "features": keys,
        "train_rows": len(train),
        "test_rows": len(test),
        "holdout_models": holdout,
        "train_block_share": float(train_y.mean()),
        "test_block_share": float(test_y.mean()),
        "rungs": rungs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify results are current")
    arguments = parser.parse_args()

    fresh = json.dumps(run(), indent=2, sort_keys=True) + "\n"
    if arguments.check:
        current = RESULTS.read_text(encoding="utf-8") if RESULTS.exists() else ""
        if current != fresh:
            print(f"{RESULTS} is stale; rerun scripts/train_preflight.py")
            return 1
        print(f"{RESULTS} is current")
        return 0

    RESULTS.write_text(fresh, encoding="utf-8")
    result = json.loads(fresh)
    print(f"wrote {RESULTS.relative_to(ROOT)}")
    print(
        f"  split: {result['train_rows']:,} train / {result['test_rows']:,} test "
        f"across {len(result['holdout_models'])} held-out models"
    )
    for rung in result["rungs"]:
        print(
            f"  {rung['rung']:28s} FN={rung['false_negative_rate']:6.2%}  "
            f"abstain={rung['abstention_rate']:6.2%}  "
            f"acc={rung['accuracy']:6.2%}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
