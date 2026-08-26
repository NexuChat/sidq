#!/usr/bin/env python3
"""Train the reader that turns catalog prose into a proposed claim.

`docs/PREFLIGHT.md` §1 says a model is legitimate here only where no
deterministic algorithm exists. Deciding a verdict has one, which is why the
engine has no model in it. *Reading a sentence a human wrote* does not, which is
why this exists — and the two facts are the same architectural boundary seen
from its two sides. `sidq.claims.attest` enforces it: whatever this produces is
a proposal that still has to survive read-only SQL against the live source.

Three things make this a different exercise from the pre-flight ladder:

**The task is classification, not generation.** A local generative model was
tried first and abstained on every positive row of the held-out set — asking a
sub-billion-parameter model to emit conforming JSON is not the shape of this
problem. What the problem actually is: given one sentence and a column name,
which of five claim types is being asserted, or none.

**It is multilingual by construction.** A catalog's descriptions are written in
whatever language its team speaks, and `tests/fixtures/catalog_corpus.py`
already exercises eight of them. A multilingual embedding model reads all of
them; a regular expression reads one.

**It can only propose what it can fully specify.** `accepted_values` needs the
value list, `relationships` and `expression` need an expression — arguments a
classifier does not produce. So those types are learned and reported, but never
proposed at inference. Half-specified claims would compile into queries that
test something the documentation never said.

Usage:
    scripts/train_claim_reader.py --embed        # GPU machine: cache embeddings
    scripts/train_claim_reader.py --fit          # train + evaluate + write head
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "claims"
ARTIFACTS = ROOT / "data" / "claims" / "reader"
EMBEDDING_MODEL = "microsoft/harrier-oss-v1-270m"
EMBEDDING_REVISION = "31de22b673913c7d658c0f03f792d77c2dcf8ebd"

# The label space. `none` is a real class, not a leftover: a documentation
# sentence that asserts nothing checkable is the common case, and a reader that
# cannot say so confidently is worse than no reader.
LABELS = (
    "none",
    "unique",
    "not_null",
    "accepted_values",
    "relationships",
    "expression",
)
# Only these two are fully specified by their type alone, so only these are ever
# proposed. The rest need arguments a classifier does not produce.
PROPOSABLE = ("unique", "not_null")

# Inverse regularisation strength, weakest-regularisation last. Selection walks
# this on the calibration split alone; the reported figure never sees it.
REGULARISATION_LADDER = (1.0, 3.0, 10.0, 30.0, 100.0)


def _rows(split: str) -> list[dict]:
    with (CORPUS / f"{split}.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _label(row: dict) -> str:
    claim = (row.get("target") or {}).get("claim")
    if not isinstance(claim, dict):
        return "none"
    kind = claim.get("type")
    return kind if kind in LABELS else "none"


def _text(row: dict) -> str:
    """What the reader sees: the sentence, and the column it describes.

    The column name is part of the input because the same sentence means
    different things attached to different columns — "one row per customer" is a
    uniqueness claim about `customer_id` and says nothing about `country`.
    """
    supplied = row.get("input") or {}
    column = supplied.get("column_name") or ""
    table = supplied.get("table_name") or ""
    return f"column: {column}\ntable: {table}\n{supplied.get('sentence', '')}"


def embed() -> None:
    """Cache sentence embeddings. Run where a GPU is; the fit needs none."""
    import numpy
    from sentence_transformers import SentenceTransformer

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(
        EMBEDDING_MODEL,
        revision=EMBEDDING_REVISION,
        trust_remote_code=True,
    )
    for split in ("train", "eval"):
        rows = _rows(split)
        vectors = model.encode(
            [_text(row) for row in rows],
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        numpy.savez(
            ARTIFACTS / f"{split}-embeddings.npz",
            x=numpy.asarray(vectors, dtype="float32"),
            y=numpy.array([LABELS.index(_label(row)) for row in rows]),
        )
        print(f"{split}: {len(rows)} rows -> {vectors.shape}")


def rule_baseline() -> dict[str, float]:
    """The deterministic reader, scored exactly like the trained one.

    Published because "the model beats the rules" is a claim, and a claim needs
    the two candidates measured the same way on the same held-out rows. It is
    also the honest counterweight to `docs/DECISION-COST.md`: on *deciding* a
    verdict the rule wins outright, and it would be selective reporting to show
    that comparison and not this one.
    """
    from sidq.claims.extractor import RuleBasedExtractor

    reader = RuleBasedExtractor()
    proposable = set(PROPOSABLE)
    made = correct = could = 0
    for row in _rows("eval"):
        supplied = row.get("input") or {}
        truth = _label(row)
        could += int(truth in proposable)
        claim = reader.extract(
            supplied.get("sentence", ""), supplied.get("column_name", ""), {}
        )
        if claim is None or claim.type not in proposable:
            continue
        made += 1
        correct += int(claim.type == truth)
    return {
        "proposals": float(made),
        "precision": (correct / made) if made else 0.0,
        "recall": (correct / could) if could else 0.0,
    }


def fit(threshold_target: float) -> dict:
    """Train both heads, keep the better one, and choose an operating point.

    Two heads are trained because the cheaper one winning is a real outcome, and
    the same rule as `docs/PREFLIGHT-RESULTS.md` applies: a tie resolves to the
    simpler candidate. The operating point is chosen for *precision*, not
    accuracy — a wrongly-typed proposal compiles into a query that tests
    something the documentation never claimed, and a finding from that query
    would be a fabricated one.
    """
    import numpy
    from sklearn.linear_model import LogisticRegression

    # CatBoost is the challenger, and it is not in any lockfile. Importing it at
    # the top made `--fit` unrunnable in every environment this repository pins,
    # so the figure it produces could not be re-derived by anyone who installed
    # what the locks say to install. The challenger is now optional: without it
    # the ladder is one rung shorter, the report says so, and the run completes.
    # Nothing about the outcome changes — the comment below records that the
    # simplest candidate won on merit, and the shipped head is that one.
    try:
        from catboost import CatBoostClassifier
    except ImportError:
        CatBoostClassifier = None  # type: ignore[assignment]

    train = numpy.load(ARTIFACTS / "train-embeddings.npz")
    held = numpy.load(ARTIFACTS / "eval-embeddings.npz")
    all_x, all_y = train["x"], train["y"]
    eval_x, eval_y = held["x"], held["y"]

    # Three splits, not two. The held-out set used to do three jobs at once: it
    # chose the head, it chose the threshold, and it supplied the number we
    # published. A figure that is the maximum of a search over the same rows it
    # is then reported on is an upper bound of a selection, not a measurement,
    # and the honest word for it is optimistic. Selection now happens on rows
    # carved out of training — unseen by the fit, so the threshold is not chosen
    # where the head is strongest — and `eval` is touched once, at the end, at a
    # threshold that was already fixed.
    #
    # The carve is deterministic and stratified by construction rather than
    # seeded: every fourth row *within each label* goes to calibration. No RNG to
    # pin, the same split on any machine, and no label can vanish from it.
    fit_mask, calibration_mask = _stratified_quarter(all_y)
    fit_x, fit_y = all_x[fit_mask], all_y[fit_mask]
    calibration_x, calibration_y = all_x[calibration_mask], all_y[calibration_mask]

    # A ladder of regularisation strengths rather than one arbitrary default.
    # Sweeping this only became worth doing once selection moved off the reported
    # rows: tuning against the figure you then publish raises the figure without
    # raising the reader. Declared least-confident-first, so a tie is broken
    # toward the more regularised head.
    candidates: dict[str, object] = {
        f"logistic regression (C={strength})": LogisticRegression(
            max_iter=8000, random_state=0, C=strength
        ).fit(fit_x, fit_y)
        for strength in REGULARISATION_LADDER
    }
    if CatBoostClassifier is not None:
        candidates["catboost"] = CatBoostClassifier(
            iterations=400, depth=6, learning_rate=0.1, verbose=0, random_seed=0
        ).fit(fit_x, fit_y)

    proposable = [LABELS.index(name) for name in PROPOSABLE]
    report: dict[str, object] = {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_revision": EMBEDDING_REVISION,
        "train_rows": len(fit_y),
        "calibration_rows": len(calibration_y),
        "eval_rows": len(eval_y),
        "labels": list(LABELS),
        "proposable": list(PROPOSABLE),
        "rule_baseline": rule_baseline(),
        "challenger_available": CatBoostClassifier is not None,
        "candidates": {},
    }

    scored = []
    for name, head in candidates.items():
        probabilities = head.predict_proba(calibration_x)  # type: ignore[attr-defined]
        point = _operating_point(
            probabilities, calibration_y, proposable, threshold_target
        )
        accuracy = float((probabilities.argmax(axis=1) == calibration_y).mean())
        assert isinstance(report["candidates"], dict)
        report["candidates"][name] = {"accuracy": accuracy, **point}
        scored.append((name, head, point))

    # The precision target is a bar to clear, not a quantity to maximise — the
    # first version of this maximised it and picked the boosted head for being
    # 0.4 points ahead on fifty-odd samples, which is noise, while giving up
    # sixteen points of recall and adding a training-stack dependency to
    # inference. So: among the candidates that clear the bar, take the simplest,
    # which is the same rule `docs/PREFLIGHT-RESULTS.md` §4 applies to the
    # pre-flight ladder. Candidates are declared simplest-first.
    # Among the candidates that clear the precision bar, take the one that speaks
    # most — "a reader tuned to perfect precision by never speaking is not a
    # reader", and the bar is a bar, not a quantity to maximise. Ties go to the
    # more regularised head, which is why the ladder is declared in that order.
    # If nothing clears it, fall back to the most precise and let the report show
    # a bar that was not met rather than quietly relabelling the target.
    clearing = [item for item in scored if item[2]["precision"] >= threshold_target]
    best_name, best_head, best_point = (
        max(clearing, key=lambda item: item[2]["recall"])
        if clearing
        else max(scored, key=lambda item: item[2]["precision"])
    )
    report["chosen"] = best_name
    report["calibration_point"] = best_point

    # Selection is finished, so the quarter held back for it goes back to work:
    # the chosen head is refit on every training row before it ships. Measured
    # here, keeping a quarter of the corpus unused cost 0.7 points of precision
    # and 6.7 of recall — a real loss, paid for nothing, since the threshold was
    # already fixed and cannot be re-tuned by seeing more data.
    #
    # The assumption this makes, stated rather than buried: a threshold
    # calibrated against a head fit on 3/4 of the rows is applied to a head fit
    # on all of them, and refitting shifts the score distribution slightly. It is
    # the standard select-then-refit contract, and the alternative — publishing a
    # figure from a deliberately weakened model — is worse in both directions.
    best_head = _refit(best_name, all_x, all_y)
    report["refit_rows"] = len(all_y)

    # The published figure: measured once, at a threshold that was already fixed,
    # on rows selection never touched. Nothing is searched here — searching is
    # exactly what made the previous number a ceiling rather than a measurement.
    report["operating_point"] = _measure_at(
        best_head.predict_proba(eval_x),  # type: ignore[attr-defined]
        eval_y,
        proposable,
        float(best_point["threshold"]),
    )
    report["selection_note"] = (
        "The threshold and the head were chosen on `calibration_rows`, carved out "
        "of training and unseen by the fit. `operating_point` is measured on "
        "`eval_rows`, which selection never touched. The head then ships refit on "
        "all `refit_rows`. `calibration_point` is kept beside the published one so "
        "the gap between them is visible rather than absorbed."
    )

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _save_head(best_name, best_head, best_point["threshold"])
    (ARTIFACTS / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def _refit(name: str, x, y):
    """Refit the chosen candidate on every training row, selection being over."""
    from sklearn.linear_model import LogisticRegression

    if name.startswith("logistic regression"):
        strength = float(name.rsplit("C=", 1)[1].rstrip(")"))
        return LogisticRegression(max_iter=8000, random_state=0, C=strength).fit(x, y)
    from catboost import CatBoostClassifier

    return CatBoostClassifier(
        iterations=400, depth=6, learning_rate=0.1, verbose=0, random_seed=0
    ).fit(x, y)


def _stratified_quarter(labels):
    """Split row indices into (fit, calibration), a quarter held for selection.

    Deterministic without a seed: rows are walked in order and every fourth one
    *within its own label* is taken. That keeps the calibration split stratified
    by construction — a rare label cannot be absent from it by luck — and makes
    the split reproducible on any machine without pinning an RNG.
    """
    import numpy

    counts: dict[int, int] = {}
    calibration = numpy.zeros(len(labels), dtype=bool)
    for index, label in enumerate(labels):
        key = int(label)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] % 4 == 0:
            calibration[index] = True
    return ~calibration, calibration


def _measure_at(
    probabilities, truth, proposable: list[int], threshold: float
) -> dict[str, float]:
    """Precision and recall at a threshold that is already fixed.

    Deliberately not a search. `_operating_point` looks for the best threshold,
    which is the right thing to do on calibration rows and the wrong thing to do
    on the rows a published figure comes from — a maximum over a search is a
    ceiling, not a measurement.
    """
    import numpy

    could = float(sum(1 for label in truth if label in proposable))
    made = correct = 0
    for row, actual in zip(probabilities, truth, strict=True):
        predicted = int(numpy.argmax(row))
        if predicted not in proposable or row[predicted] < threshold:
            continue
        made += 1
        correct += int(predicted == actual)
    return {
        "threshold": threshold,
        "precision": (correct / made) if made else 0.0,
        "recall": (correct / could) if could else 0.0,
        "proposals": float(made),
    }


def _operating_point(
    probabilities, truth, proposable: list[int], target: float
) -> dict[str, float]:
    """The lowest threshold whose precision on proposals clears the target.

    Precision here is over *proposals actually made*: of the sentences this
    reader chose to speak about, how many did it type correctly. Recall is over
    the sentences it could have proposed. Both are reported, because a reader
    tuned to perfect precision by never speaking is not a reader.
    """
    import numpy

    best = {"threshold": 1.0, "precision": 0.0, "recall": 0.0, "proposals": 0.0}
    could = float(sum(1 for label in truth if label in proposable))
    for threshold in [value / 100 for value in range(30, 100)]:
        made = correct = 0
        for row, actual in zip(probabilities, truth, strict=True):
            predicted = int(numpy.argmax(row))
            if predicted not in proposable or row[predicted] < threshold:
                continue
            made += 1
            correct += int(predicted == actual)
        if not made:
            continue
        precision = correct / made
        point = {
            "threshold": threshold,
            "precision": precision,
            "recall": (correct / could) if could else 0.0,
            "proposals": float(made),
        }
        if precision >= target:
            return point
        if precision > best["precision"]:
            best = point
    return best


def _save_head(name: str, head, threshold: float) -> None:
    """Persist the head so inference needs no training stack.

    Only linear coefficients are exported. A boosted head that wins would be
    saved in its own format instead; keeping the exported artifact plain is what
    lets the shipped extractor depend on nothing but numpy.
    """
    import numpy

    if name.startswith("logistic regression"):
        numpy.savez(
            ARTIFACTS / "head.npz",
            coef=head.coef_,
            intercept=head.intercept_,
            classes=head.classes_,
            threshold=numpy.array([threshold]),
        )
    else:
        head.save_model(str(ARTIFACTS / "head.cbm"))
        (ARTIFACTS / "head-threshold.json").write_text(
            json.dumps({"threshold": threshold}) + "\n", encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embed", action="store_true", help="cache embeddings")
    parser.add_argument("--fit", action="store_true", help="train and evaluate")
    parser.add_argument(
        "--precision",
        type=float,
        default=0.95,
        help="the precision the operating point must clear",
    )
    arguments = parser.parse_args()

    if arguments.embed:
        embed()
    if arguments.fit:
        report = fit(arguments.precision)
        print(json.dumps(report, indent=2, sort_keys=True))
    if not (arguments.embed or arguments.fit):
        parser.error("choose --embed or --fit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
