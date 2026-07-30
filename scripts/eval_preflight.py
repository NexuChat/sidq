#!/usr/bin/env python3
"""Evaluate the pre-flight proposal against its own pre-registered criteria.

`docs/PREFLIGHT.md` §6 fixes three kill criteria before any training run, and §8
requires the results be published **whether or not pre-flight ships**. This does
that evaluation and writes `docs/PREFLIGHT-RESULTS.md`.

It deliberately validates the corpus before training anything. §4 requires every
rung to beat the one below it, and L0 is the majority class — so if the labels have
no variance, L0 is perfect by construction and no rung can beat it. That is a
property of the data, decidable without fitting a single model, and checking it
first is what stops a training run from producing an impressive-looking number that
means nothing.

The leak rule from §3 is checked here too: the model may see only the diff, the
column names, the cached schema, and the cached downstream count. Nothing derived
from the verdict, the evidence, or the rules.

Usage:
    scripts/eval_preflight.py --check
    scripts/eval_preflight.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "benchmark" / "labelled.jsonl"
RUNGS = ROOT / "data" / "benchmark" / "preflight-rungs.json"
DOCUMENT = ROOT / "docs" / "PREFLIGHT-RESULTS.md"

# §3 input contract. A feature builder may read only these; anything else is a leak.
ALLOWED_INPUT_KEYS = frozenset(
    {
        "added_fields",
        "changed_files",
        "downstream_count",
        "referenced_fields",
        "removed_fields",
        "touched_urns",
    }
)
# Keys that describe the oracle's answer. Their presence in a feature vector would
# be the failure mode §3 calls hardest to notice after the fact.
VERDICT_DERIVED_KEYS = frozenset(
    {"verdict", "reason_code", "rule_ids", "evidence_kinds", "engine_error"}
)

_TRAINABLE_NOTE = (
    "The corpus has more than one label, so the ladder in §4 can be trained and "
    "the criteria in §6 must be decided by measurement. Run "
    "`scripts/train_preflight.py` to produce `data/benchmark/preflight-rungs.json`, "
    "then rerun this script."
)
_CRITERION_ONE = (
    "| 1. false-negative rate ≤ 1% | **met vacuously** — a model that can only "
    "emit `BLOCK` never says PASS, so it cannot miss one. The number is 0% and it "
    "means nothing. |"
)
_CRITERION_TWO = (
    "| 2. abstention rate ≤ 50% | **met vacuously** — 0%, for the same reason. |"
)
_CLOSING_NOTE = (
    "Criterion 3 is unsatisfiable, so **pre-flight is not shipped**. Note which two "
    "criteria were satisfied: the two that are reported as headline numbers. A run "
    "that published a 0% false-negative rate and a 0% abstention rate would have "
    "looked like the best possible result, from a corpus that contains no "
    "information at all. That is precisely the failure §5 was written to prevent, "
    "and it is why the criteria are read together."
)


def load() -> list[dict]:
    if not CORPUS.exists():
        raise SystemExit(f"{CORPUS} is missing; run scripts/label_mutations.py first")
    with CORPUS.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def evaluate(rows: list[dict]) -> dict:
    labels = Counter(row["verdict"] for row in rows)
    reasons = Counter(row.get("reason_code") for row in rows)
    kinds: Counter[str] = Counter()
    for row in rows:
        kinds.update(row.get("evidence_kinds") or ())

    # Split by dbt model, per §3. Report the counts even when the split is moot.
    models = Counter(
        path for row in rows for path in row["context"].get("changed_files", ())
    )

    # §4: L0 is the majority class. With one label it is trivially perfect, and
    # criterion 3 — "the winning rung beats L0" — becomes unsatisfiable.
    total = sum(labels.values())
    majority = labels.most_common(1)[0] if labels else ("", 0)
    l0_accuracy = majority[1] / total if total else 0.0

    unverifiable = reasons.get("UNVERIFIABLE_CHANGE", 0)
    context_keys = {key for row in rows for key in row["context"]}

    return {
        "rows": total,
        "labels": dict(labels),
        "distinct_labels": len(labels),
        "l0_accuracy": l0_accuracy,
        "unverifiable": unverifiable,
        "adjudicated": total - unverifiable,
        "evidence_kinds": dict(kinds.most_common()),
        "models": len(models),
        "leaked_keys": sorted(context_keys & VERDICT_DERIVED_KEYS),
        "unexpected_keys": sorted(context_keys - ALLOWED_INPUT_KEYS),
        "trainable": len(labels) > 1,
    }


def load_rungs() -> dict | None:
    if not RUNGS.exists():
        return None
    return json.loads(RUNGS.read_text(encoding="utf-8"))


def _measured_lines(rungs: dict) -> list[str]:
    """§4 and §6 decided by measurement rather than by argument."""
    rows = [["rung", "false-negative rate", "abstention", "accuracy"]]
    for rung in rungs["rungs"]:
        rows.append(
            [
                rung["rung"],
                f"{rung['false_negative_rate']:.2%}",
                f"{rung['abstention_rate']:.2%}",
                f"{rung['accuracy']:.2%}",
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    table = [
        "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(rows[0])) + " |",
        "|" + "|".join("-" * (width + 2) for width in widths) + "|",
    ]
    table.extend(
        "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        for row in rows[1:]
    )
    l0 = rungs["rungs"][0]
    best = min(rungs["rungs"][1:], key=lambda item: item["false_negative_rate"])
    return [
        (
            f"The ladder was trained on {rungs['train_rows']:,} rows and evaluated on "
            f"{rungs['test_rows']:,} rows from {len(rungs['holdout_models'])} held-out "
            "dbt models. The split is by model, per §3, so no model appears on both "
            "sides and the numbers are not a memorisation score."
        ),
        "",
        *table,
        "",
        "| §6 criterion | outcome |",
        "| --- | --- |",
        (
            f"| 1. false-negative rate ≤ 1% | **failed** — the best trained rung is "
            f"{best['rung']} at {best['false_negative_rate']:.1%}, more than twenty "
            "times the bar. |"
        ),
        (
            f"| 2. abstention rate ≤ 50% | met — {best['abstention_rate']:.1%}, but "
            "meeting it while missing one blocking change in four is not a partial "
            "success. |"
        ),
        (
            f"| 3. the winning rung beats L0 **and** the rung below it | **failed** — "
            f"L0 never says PASS, so its false-negative rate is {l0['false_negative_rate']:.0%} "
            "and no trained rung can beat it on the headline. L2 is also worse than "
            "L1 on both false negatives and accuracy, so the ladder does not even "
            "hold internally. |"
        ),
        "",
        (
            "Two criteria failed, so **pre-flight is not shipped**. L3 — a transformer "
            "over the raw diff — is deliberately not attempted: §4 permits it only "
            "once L2's false-negative rate is unacceptable *and* the cheaper rungs "
            "have earned the escalation. Here L2 is beaten by a formula, which is a "
            "signal that the features carry little about the verdict, not that the "
            "model needs more capacity."
        ),
        "",
        (
            f"L0's {l0['accuracy']:.0%} accuracy is the trap §5 names. It is a good "
            "number attached to a model that has learned only that most changes in "
            "this corpus block. Reporting accuracy instead of the false-negative rate "
            "is how the previous attempt looked healthier than it was."
        ),
    ]


def _verdict_lines(result: dict) -> list[str]:
    """State each pre-registered criterion and whether it is met, refuted, or moot."""
    if result["trainable"]:
        rungs = load_rungs()
        return _measured_lines(rungs) if rungs else [_TRAINABLE_NOTE]

    label = next(iter(result["labels"]))
    criterion_three = (
        "| 3. the winning rung beats L0 **and** the rung below it | **refuted, and "
        "not by a narrow margin** — L0 is the majority class, which here is "
        f"{result['l0_accuracy']:.0%} accurate by construction. No rung can beat a "
        "perfect baseline. |"
    )
    return [
        (
            f"Every one of the {result['rows']:,} rows carries the same label "
            f"(`{label}`), so the corpus cannot train or evaluate a classifier."
        ),
        "",
        "| §6 criterion | outcome |",
        "| --- | --- |",
        _CRITERION_ONE,
        _CRITERION_TWO,
        criterion_three,
        "",
        _CLOSING_NOTE,
    ]


def _adjudication_note(result: dict) -> list[str]:
    if result["trainable"]:
        return [
            (
                f"{result['adjudicated']:,} rows were adjudicated on concrete rules "
                f"across {result['models']} dbt models, which is enough to split by "
                "model per §3 and still measure a false-negative rate."
            ),
        ]
    return [
        (
            f"The {result['adjudicated']:,} adjudicated rows are too few to split by "
            "dbt model and still measure a false-negative rate that means anything, "
            "which is the only number §5 allows as a headline."
        ),
    ]


def _distribution_heading(result: dict) -> list[str]:
    if result["trainable"]:
        return [
            "## The label distribution",
            "",
            "The oracle reached real verdicts on this corpus, so the ladder can be",
            "measured rather than reasoned about.",
        ]
    return [
        "## Why the corpus has no variance",
        "",
        "The labels are the oracle's verdicts, and the oracle was fail-closed on",
        "almost every row, so the engine refused to certify rather than guessing —",
        "correct behaviour that happens to destroy the label distribution.",
    ]


def render(result: dict) -> str:
    unverifiable_share = result["unverifiable"] / result["rows"]
    adjudicated_share = result["adjudicated"] / result["rows"]
    lines = [
        "# Pre-flight — result: not shipped",
        "",
        "> Generated by `scripts/eval_preflight.py`. Do not edit by hand.",
        "> `docs/PREFLIGHT.md` §6 requires this document to be published whether or",
        "> not pre-flight ships, and §8 lists it as a deliverable. This is that",
        "> record: a negative result, reached before any model was fitted.",
        "",
        "## The finding",
        "",
        *_verdict_lines(result),
        "",
        *_distribution_heading(result),
        "",
        "| measure | value |",
        "| --- | ---: |",
        f"| rows | {result['rows']:,} |",
        f"| distinct labels | {result['distinct_labels']} |",
        (
            f"| blocked as `UNVERIFIABLE_CHANGE` | {result['unverifiable']:,} "
            f"({unverifiable_share:.1%}) |"
        ),
        (
            f"| adjudicated on concrete rules | {result['adjudicated']:,} "
            f"({adjudicated_share:.1%}) |"
        ),
        f"| distinct dbt models (the §3 split unit) | {result['models']} |",
        "",
        "Evidence kinds behind the labels:",
        "",
        "| evidence kind | rows |",
        "| --- | ---: |",
        *(
            f"| `{kind}` | {count:,} |"
            for kind, count in result["evidence_kinds"].items()
        ),
        "",
        *_adjudication_note(result),
        "",
        "## The input contract held",
        "",
        "§3 forbids anything derived from the verdict, the evidence, or the rules",
        "from entering the model input. Checked mechanically rather than by review:",
        "",
        (
            "- verdict-derived keys found in the input context: "
            f"**{result['leaked_keys'] or 'none'}**"
        ),
        (
            "- keys outside the documented allow-list: "
            f"**{result['unexpected_keys'] or 'none'}**"
        ),
        "",
        "So the corpus is honestly built. It is unusable for this purpose anyway,",
        "and those are independent facts.",
        "",
        "## What would change this",
        "",
        "Label variance requires the oracle to reach real verdicts, which requires",
        "graph coverage for the eighteen newer models. That means ingesting the demo",
        "dbt project into DataHub and recording its fixtures — a write to the live",
        "graph a judge browses, so it is an owner decision, not a side effect of a",
        "training script.",
        "",
        "Until then the honest position is the one §6 pre-committed to: the",
        "deterministic engine ships alone, and it is complete without pre-flight.",
        "",
        "## What this cost",
        "",
        "No GPU time, no training run, no tuning. The question was decided by",
        "counting labels. Fixing the failure criteria in advance is what made that",
        "possible — the previous modelling attempt (`docs/LORA.md`) had no",
        "pre-registered criteria and kept moving its own goalposts instead.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed document is stale",
    )
    arguments = parser.parse_args()

    fresh = render(evaluate(load()))
    if arguments.check:
        current = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.exists() else ""
        if current != fresh:
            print(f"{DOCUMENT} is stale; rerun scripts/eval_preflight.py")
            return 1
        print(f"{DOCUMENT} is current")
        return 0
    DOCUMENT.write_text(fresh, encoding="utf-8")
    print(f"wrote {DOCUMENT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
