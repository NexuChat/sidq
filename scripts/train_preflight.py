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

from sidq.resolver import _sql_parts

CORPUS = ROOT / "data" / "benchmark" / "labelled.jsonl"
MUTATIONS = ROOT / "data" / "benchmark" / "mutations.jsonl"
FIXTURES = ROOT / "tests" / "fixtures" / "graph"
RESULTS = ROOT / "data" / "benchmark" / "preflight-rungs.json"
SEED = 0
# §5: false negatives are the dangerous error, false positives are tolerable. The
# operating point encodes that asymmetry directly — PASS is only emitted when the
# model is at least this confident the change passes, while BLOCK needs no such
# margin, because being wrong in that direction costs one oracle call.
#
# A data-chosen threshold was tried and is recorded as a negative result. Picking
# the PASS bar as the lowest probability assigned to any validation block is a
# fragile statistic on a model-disjoint validation split: a handful of models where
# the classifier happens to be confident drags the bar upward, and everything below
# it is waved through. It moved L1 from 0.27% false negatives to 14.51%, and to
# 16.13% after the validation split was itself corrected to be model-disjoint. The
# fixed conservative bar below beats both, so it is what ships.
PASS_CONFIDENCE = 0.10


def load_rows() -> list[dict]:
    """Join each label to its diff. Both are §3 inputs; the family is not.

    `family` and `intent` are generator metadata that would not exist for a real
    change, so they are dropped here rather than merely left unused — a feature
    cannot accidentally reach what the record does not carry.
    """
    diffs: dict[str, str] = {}
    sql: dict[str, str] = {}
    with MUTATIONS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            diffs[str(record["id"])] = str(record.get("diff") or "")
            sql[str(record["id"])] = str(record.get("mutated_sql") or "")
    rows = []
    with CORPUS.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            rows.append(
                {
                    "id": record["id"],
                    "verdict": record["verdict"],
                    "context": record["context"],
                    "diff": diffs.get(str(record["id"]), ""),
                    "sql": sql.get(str(record["id"]), ""),
                }
            )
    return rows


class SchemaCache:
    """The touched asset's cached schema — a §3 input a local pre-filter has.

    Reads the committed graph replay snapshot, which is exactly what "cached"
    means here: no network call, no oracle, no evidence.
    """

    def __init__(self) -> None:
        from sidq.graph.fixtures import ReplayGraphClient

        self._client = ReplayGraphClient(FIXTURES)
        self._cache: dict[str, frozenset[str]] = {}

    def fields(self, urn: str) -> frozenset[str]:
        if urn not in self._cache:
            try:
                dataset = self._client.get_dataset(urn)
            except Exception:
                dataset = None
            self._cache[urn] = (
                frozenset(field.path for field in dataset.fields) if dataset else frozenset()
            )
        return self._cache[urn]


def features(context: dict, diff: str, schema: SchemaCache) -> dict[str, float]:
    """Build the §3 input vector: change context, the diff, the cached schema.

    Given only those three, never the record, so a verdict-derived feature is not
    merely forbidden — it is unreachable. The first version of this builder used
    counts alone and missed one blocking change in four. The features that matter
    are the ones a rule actually keys on: `unknown_field` fires when a referenced
    column is absent from the cached schema, which is computable here without any
    network call at all.
    """
    added = context.get("added_fields") or []
    removed = context.get("removed_fields") or []
    referenced = [str(item) for item in (context.get("referenced_fields") or [])]
    files = [str(item) for item in (context.get("changed_files") or [])]
    urns = context.get("touched_urns") or []

    # The `unknown_field` signal: a referenced column the cached schema does not have.
    unknown = 0
    resolvable = 0
    for reference in referenced:
        urn, _, field = reference.rpartition("#")
        if not urn or not field:
            continue
        known = schema.fields(urn)
        if not known:
            continue
        resolvable += 1
        if field not in known:
            unknown += 1

    touched_fields: frozenset[str] = frozenset()
    for urn in urns:
        touched_fields |= schema.fields(str(urn))

    lines = diff.splitlines()
    added_lines = sum(1 for line in lines if line.startswith("+") and line[1:2] != "+")
    removed_lines = sum(1 for line in lines if line.startswith("-") and line[1:2] != "-")
    upper = diff.upper()
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
        # Schema-derived: the strongest available predictor of `unknown_field`.
        "unknown_referenced": float(unknown),
        "has_unknown_referenced": 1.0 if unknown else 0.0,
        "unknown_share": unknown / resolvable if resolvable else 0.0,
        "schema_known": float(len(touched_fields)),
        "schema_missing": 1.0 if urns and not touched_fields else 0.0,
        "added_not_in_schema": float(
            sum(1 for name in added if str(name) not in touched_fields)
        ),
        "removed_in_schema": float(
            sum(1 for name in removed if str(name) in touched_fields)
        ),
        # Diff-derived: shape of the edit, which separates formatting from surgery.
        "diff_added_lines": float(added_lines),
        "diff_removed_lines": float(removed_lines),
        "diff_churn": float(added_lines + removed_lines),
        "diff_balanced": 1.0 if added_lines == removed_lines else 0.0,
        "diff_has_cast": 1.0 if "CAST(" in upper else 0.0,
        "diff_has_star": 1.0 if "SELECT *" in upper else 0.0,
        "diff_has_join": 1.0 if "JOIN" in upper else 0.0,
        "diff_has_group_by": 1.0 if "GROUP BY" in upper else 0.0,
        "diff_has_where": 1.0 if "WHERE" in upper else 0.0,
        "mentions_email": 1.0 if "email" in text else 0.0,
        "mentions_id": 1.0 if "_id" in text else 0.0,
        "is_staging": 1.0 if any("staging/" in name for name in files) else 0.0,
        "is_mart": 1.0 if any("marts/" in name for name in files) else 0.0,
        "is_intermediate": (
            1.0 if any("intermediate/" in name for name in files) else 0.0
        ),
    }


def model_of(row: dict) -> str:
    return str((row["context"].get("changed_files") or ["?"])[0])


def split_by_model(
    rows: list[dict], *, every: int = 3, offset: int = 0
) -> tuple[list[dict], list[dict], list[str]]:
    """Hold out whole dbt models, per §3. Deterministic, no shuffling."""
    models = sorted({model_of(row) for row in rows})
    holdout = {
        model for index, model in enumerate(models) if index % every == offset
    }
    train = [row for row in rows if model_of(row) not in holdout]
    test = [row for row in rows if model_of(row) in holdout]
    return train, test, sorted(holdout)


def certain_block(row: dict) -> bool:
    """Decide locally what the oracle decides deterministically.

    Two of the engine's refusals need no graph and no model at all. If the changed
    file maps to no manifest asset, or its SQL does not parse, the oracle returns
    BLOCK(UNVERIFIABLE_CHANGE) every time. Asking a classifier to *learn* that is a
    category error, and it is what produced the first ladder's false negatives:
    those cases lived only in held-out models, so the feature was never trained,
    and the model guessed PASS on a refusal that was never in doubt.

    A pre-filter should apply what is decidable and model only the remainder.
    """
    urns = row["context"].get("touched_urns") or []
    if not urns:
        return True
    text = row.get("sql") or ""
    if not text.strip():
        return False
    try:
        # The engine's own analysis, not a re-implementation of it. A bare
        # `sqlglot.parse_one` was tried first and missed sixteen refusals: the
        # engine's `unparseable_sql` also covers column-resolution failures that
        # happen well after parsing succeeds — "could not resolve X through a
        # derived relation". Two implementations that are supposed to agree is
        # exactly what §7 forbids, so this calls the one the oracle calls.
        _sql_parts(text, str(urns[0]), None)
    except Exception:
        return True
    return False


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
    schema = SchemaCache()
    train, test, holdout = split_by_model(rows)
    keys = sorted(features(rows[0]["context"], rows[0]["diff"], schema))

    def matrix(subset: list[dict]) -> tuple:
        x = numpy.array(
            [
                [features(row["context"], row["diff"], schema)[key] for key in keys]
                for row in subset
            ],
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

    def with_point(probabilities) -> list[int | None]:
        """Say PASS only under the confidence bar; BLOCK otherwise (§5)."""
        return [0 if p <= PASS_CONFIDENCE else 1 for p in probabilities]

    certain = [certain_block(row) for row in test]

    def with_precheck(predictions: list[int | None]) -> list[int | None]:
        """Deterministic refusals override the model; it is never asked about them."""
        return [
            1 if sure else prediction
            for sure, prediction in zip(certain, predictions, strict=True)
        ]

    # L0.5 — the deterministic pre-checks alone, abstaining everywhere else. It is
    # published because it establishes how much of the ladder needs a model at all.
    rungs.append(
        _score(
            "L0.5 deterministic pre-checks",
            truth,
            [1 if sure else None for sure in certain],
        )
    )

    # L0.75 — the rule the trained models turn out to be imitating. Found by
    # asking what the classifier had learned rather than accepting its score: on
    # every row the pre-checks leave, the oracle blocks exactly when a referenced
    # column is missing from the cached schema or the change has any downstream
    # consumer. Published as a rung because §1's first condition — that no
    # deterministic algorithm exists — is the one that decides whether a model is
    # legitimate at all, and here it is false.
    def rule(row: dict) -> int:
        vector = features(row["context"], row["diff"], schema)
        return 1 if vector["has_unknown_referenced"] or vector["downstream"] else 0

    rungs.append(
        _score(
            "L0.75 pre-checks and a two-term rule",
            truth,
            with_precheck([rule(row) for row in test]),
        )
    )

    scaler = StandardScaler().fit(train_x)
    l1 = LogisticRegression(max_iter=2000, random_state=SEED).fit(
        scaler.transform(train_x), train_y
    )
    l1_probabilities = l1.predict_proba(scaler.transform(test_x))[:, 1]
    rungs.append(_score("L1 logistic regression", truth, with_point(l1_probabilities)))
    rungs.append(
        _score(
            "L1+ pre-checks and logistic regression",
            truth,
            with_precheck(with_point(l1_probabilities)),
        )
    )

    l2 = HistGradientBoostingClassifier(random_state=SEED).fit(train_x, train_y)
    l2_probabilities = l2.predict_proba(test_x)[:, 1]
    rungs.append(_score("L2 gradient-boosted trees", truth, with_point(l2_probabilities)))
    rungs.append(
        _score(
            "L2+ pre-checks and gradient-boosted trees",
            truth,
            with_precheck(with_point(l2_probabilities)),
        )
    )

    return {
        "seed": SEED,
        "pass_confidence": PASS_CONFIDENCE,
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
