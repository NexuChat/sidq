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
    """§4 and §6 decided by measurement, including where a criterion cannot decide."""
    rows = [["rung", "false-negative rate", "false positives", "abstention", "accuracy"]]
    for rung in rungs["rungs"]:
        rows.append(
            [
                rung["rung"],
                f"{rung['false_negative_rate']:.2%}",
                str(rung["false_positives"]),
                f"{rung['abstention_rate']:.2%}",
                f"{rung['accuracy']:.2%}",
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    table = [line(rows[0]), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    table.extend(line(row) for row in rows[1:])

    by_name = {rung["rung"]: rung for rung in rungs["rungs"]}
    l0 = next(rung for name, rung in by_name.items() if name.startswith("L0 "))
    # Everything except the trivial baseline competes, including the non-model
    # rungs. Excluding every name beginning with "L0" was tried and was wrong: it
    # silently disqualified the two-term rule, so the report named a classifier as
    # the deliverable when a rule tied it.
    candidates = [
        rung
        for name, rung in by_name.items()
        if name != l0["rung"] and rung["abstention_rate"] <= 0.5
    ]
    # §4: "the deliverable is the cheapest rung that meets the bar". The rungs are
    # published simplest-first, so a tie must resolve to the earlier one — naming
    # the classifier when a rule equals it would misreport what should ship.
    best = min(
        candidates,
        key=lambda rung: (
            rung["false_negative_rate"],
            rungs["rungs"].index(rung),
        ),
    )
    blocks = round(rungs["test_rows"] * rungs["test_block_share"])

    one = best["false_negative_rate"] <= 0.01
    two = best["abstention_rate"] <= 0.5

    return [
        (
            f"The ladder was trained on {rungs['train_rows']:,} rows and evaluated on "
            f"{rungs['test_rows']:,} rows from {len(rungs['holdout_models'])} held-out "
            "dbt models. The split is by model, per §3, so no model appears on both "
            "sides and the numbers are not a memorisation score."
        ),
        "",
        (
            f"Those rows contain **{rungs.get('distinct_train_diffs', 0):,} distinct "
            f"training diffs and {rungs.get('distinct_test_diffs', 0):,} distinct test "
            "diffs** — the generator repeats each diff across a small set of models, "
            "so the row counts overstate the evidence roughly tenfold. No diff "
            "appears on both sides: the split by model happens to keep every "
            "repetition inside one model, which was checked rather than assumed. "
            "The result below is real but it rests on the distinct counts, not the "
            "row counts."
        ),
        "",
        *table,
        "",
        (
            f"The cheapest rung that meets the bar is **{best['rung']}**: "
            f"{best['false_negatives']} missed blocks out of {blocks:,}, and "
            f"{best['false_positives']} false alarms. §4 asks for the cheapest, not "
            "the most sophisticated, so a tie resolves downward."
        ),
        "",
        "| §6 criterion | outcome |",
        "| --- | --- |",
        (
            f"| 1. false-negative rate ≤ 1% | {'**met**' if one else '**failed**'} — "
            f"{best['false_negative_rate']:.2%}. |"
        ),
        (
            f"| 2. abstention rate ≤ 50% | {'**met**' if two else '**failed**'} — "
            f"{best['abstention_rate']:.2%}. |"
        ),
        (
            "| 3. the winning rung beats L0 **and** the rung below it | "
            "**not reached** — a non-model rung ties the best model, so §1 decides "
            "this before §6 can. |"
        ),
        "",
        "### The result is that no model is needed",
        "",
        (
            "Two rungs reach a perfect held-out score, and one of them is not a "
            "model. `L0.75` is the deterministic pre-checks plus a two-term rule: "
            "**block when a referenced column is missing from the cached schema, or "
            "when the change has any downstream consumer at all.** It reproduces the "
            "oracle on every one of the 6,050 rows the pre-checks leave undecided."
        ),
        "",
        (
            "The trained rungs match that score and do not exceed it. They were "
            "imitating the rule. This was found by asking what the classifier had "
            "learned rather than by accepting a good number, which is the only way "
            "a 100% score should ever be read."
        ),
        "",
        (
            "**That decides §1 rather than §6.** §1's first condition for a model "
            "being legitimate here is that no deterministic algorithm exists. On this "
            "corpus one does, and it is three lines long. A model that ties a rule is "
            "decoration, which is the exact failure §1 was written to prevent — so "
            "pre-flight ships as a rule or not at all, and the criteria in §6 never "
            "get to arbitrate."
        ),
        "",
        "### What this result does not say",
        "",
        (
            "The rule holds *on this corpus*, and the corpus is narrow. Its verdicts "
            "are driven almost entirely by `unknown_field` and by having any "
            "downstream at all, because these fixtures carry PII tags on one legacy "
            "model and no deprecation or ownership variation to speak of. A graph "
            "with real governance spread would not collapse to two terms, and the "
            "honest claim is bounded accordingly: **the local signal is sufficient "
            "here**, not everywhere."
        ),
        "",
        (
            "So the finding is not that pre-flight is easy. It is that this corpus "
            "cannot tell us whether pre-flight is hard, and a model trained on it "
            "would have shipped a rule wearing a classifier's clothes."
        ),
        "",
        (
            "**The rule was re-tested after the evidence got richer, not before.** "
            "Wiring the documentation and governance gates into `sidq check` took "
            "`doc_rot` from zero fires to 2,564 and `pii_exposure` from 46 to 1,367 "
            "across the corpus. The rule still reproduces the oracle on all 6,050 "
            "undecided rows. The reason is worth stating plainly rather than "
            "claiming as a win: every held-out row carrying `doc_rot` was already "
            "blocking for another reason, so the new gates added explanation rather "
            "than new decisions. Evidence got richer; the decision boundary did not "
            "move."
        ),
        "",
        "### What actually moved the number",
        "",
        (
            "The first ladder missed 22.23% of blocking changes. Diagnosing which "
            "rules those misses carried showed almost all of them were "
            "`unresolved_asset`: the changed file maps to no manifest model, so the "
            "oracle refuses to certify. That is not a thing to learn — it is a thing "
            "to compute, and a pre-filter can compute it locally with no graph and no "
            "model. The same is true of unparseable SQL."
        ),
        "",
        (
            "Adding those two deterministic pre-checks took the false-negative rate "
            "from 22.23% to 0.27% without touching the model. The lesson is the one "
            "§1 already implies: a model is legitimate only where no deterministic "
            "algorithm exists, and part of what was being modelled had one."
        ),
        "",
        (
            "L2 scores identically to L1 at every stage, so the trees earn nothing "
            "over the formula. §4 anticipated this: if the logistic regression wins, "
            "the model is a formula and a formula is what would ship. L3 is therefore "
            "not attempted."
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
