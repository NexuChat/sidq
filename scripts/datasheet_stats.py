#!/usr/bin/env python3
"""Recompute `data/claims/DATASHEET.md` from the released corpus itself.

Every composition number a reader can check was previously typed by hand, and
they drifted: the SchemaStore lane was remined from 293 rows to 287 and nothing
downstream moved with it, so the published class mix, distinct-sentence ratio,
`accepted_values` total, and per-source table all overstated a corpus anyone
could count in one command. A datasheet that a `wc -l` can falsify is worse than
no datasheet, because it is the document whose entire job is to be checkable.

So the numbers are no longer written. They are derived from `train.jsonl` and
`eval.jsonl` here, and `--check` fails the build the moment the released files
and the document disagree.

**What this script deliberately does not compute.** Mining-side statistics —
input rows scanned, positive candidates, survival rates — come from raw
collection pools that the public release does not ship in full. They are
recorded below as historical mining results, not as anything reproducible from
this repository, and no number in the generated sections depends on them.

Run with no arguments to rewrite the document, or `--check` to fail when the
committed document no longer matches the released corpus.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "claims"
DOCUMENT = CORPUS / "DATASHEET.md"
SPLITS = ("train", "eval")

# The five constraint types the corpus labels, in the order the document reads
# them. A sixth appearing here would be a schema change, and the type table
# would surface it rather than silently dropping it.
CLAIM_TYPES = ("unique", "not_null", "accepted_values", "expression", "relationships")
CLASSES = ("positive", "negative", "hard_negative")

# The per-lane working target the corpus is measured against. It is a target,
# not an achievement, and the table says so for every type.
WORKING_FLOOR = 1500

# Human labels for the (source_release, source_kind) lanes actually present.
# A lane with no label still renders — under its raw key — because an unlabelled
# lane must not vanish from the accounting.
LANE_LABELS = {
    ("raw-v3", "dbt"): "raw-v3 / dbt + SQL DDL",
    ("schema-corpora", "schemastore"): "schema-corpora / SchemaStore",
    ("raw-v4", "fhir_r5"): "raw-v4 / FHIR R5",
    ("raw-v5", "github_clone"): "raw-v5 / application-code choices",
    ("raw-v6", "github_clone"): "raw-v6 / error-message & dictionary lane",
}


def _rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split in SPLITS:
        path = CORPUS / f"{split}.jsonl"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{number}: {error}") from error
            row["_split"] = split
            rows.append(row)
    if not rows:
        raise SystemExit("the released corpus is empty")
    return rows


def _claim_type(row: Mapping[str, Any]) -> str:
    target = row.get("target")
    claim = target.get("claim") if isinstance(target, Mapping) else None
    if not isinstance(claim, Mapping):
        return "no_claim"
    kind = claim.get("type")
    return str(kind) if kind else "no_claim"


def _sentence(row: Mapping[str, Any]) -> str:
    payload = row.get("input")
    return str(payload.get("sentence", "")) if isinstance(payload, Mapping) else ""


def _lane(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("source_release", "")), str(row.get("source_kind", ""))


def measure() -> dict[str, Any]:
    """Every published composition figure, derived from the released files."""
    rows = _rows()
    sentences = [_sentence(row) for row in rows]
    by_lane: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_lane[_lane(row)].append(row)

    lanes = []
    for key in sorted(by_lane, key=lambda item: (-len(by_lane[item]), item)):
        lane_rows = by_lane[key]
        lane_sentences = [_sentence(row) for row in lane_rows]
        types = Counter(_claim_type(row) for row in lane_rows)
        lanes.append(
            {
                "release": key[0],
                "kind": key[1],
                "label": LANE_LABELS.get(key, f"{key[0]} / {key[1]}"),
                "rows": len(lane_rows),
                "documents": len({str(row.get("source_document", "")) for row in lane_rows}),
                "distinct_sentences": len(set(lane_sentences)),
                "types": {name: types.get(name, 0) for name in ("no_claim", *CLAIM_TYPES)},
            }
        )

    classes = Counter(str(row.get("class", "")) for row in rows)
    positive_types = Counter(
        _claim_type(row) for row in rows if row.get("class") == "positive"
    )
    # Which lanes contribute each type, so a claim like "accepted_values comes
    # from three lanes" is generated rather than remembered.
    contributors: dict[str, list[dict[str, Any]]] = {}
    for name in CLAIM_TYPES:
        entries = [
            {"label": lane["label"], "rows": lane["types"][name]}
            for lane in lanes
            if lane["types"][name]
        ]
        contributors[name] = sorted(entries, key=lambda item: -int(item["rows"]))

    return {
        "rows": len(rows),
        "by_split": {split: sum(1 for row in rows if row["_split"] == split) for split in SPLITS},
        "classes": {name: classes.get(name, 0) for name in CLASSES},
        "unexpected_classes": sorted(set(classes) - set(CLASSES)),
        "distinct_sentences": len(set(sentences)),
        "documents": len({str(row.get("source_document", "")) for row in rows}),
        "lanes": lanes,
        "types": {name: positive_types.get(name, 0) for name in CLAIM_TYPES},
        "type_contributors": contributors,
        "no_claim": positive_types.get("no_claim", 0)
        + sum(1 for row in rows if row.get("class") != "positive"),
        "split_documents_overlap": sorted(
            {str(row.get("source_document", "")) for row in rows if row["_split"] == "train"}
            & {str(row.get("source_document", "")) for row in rows if row["_split"] == "eval"}
        ),
        "eval_types": sorted(
            {
                _claim_type(row)
                for row in rows
                if row["_split"] == "eval" and _claim_type(row) != "no_claim"
            }
        ),
    }


def _ratio(part: int, whole: int) -> str:
    return f"{part / whole:.2%}" if whole else "n/a"


def _row(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _render(stats: Mapping[str, Any]) -> str:
    rows = int(stats["rows"])
    lanes: Sequence[Mapping[str, Any]] = stats["lanes"]
    classes: Mapping[str, int] = stats["classes"]
    types: Mapping[str, int] = stats["types"]

    lines = [
        "# Datasheet: merged constraint-pair corpus (v7)",
        "",
        "> **Generated from the released corpus.** Every number in *Composition*,",
        "> *Per-source released accounting*, and *Type coverage* is recomputed from",
        "> `train.jsonl` and `eval.jsonl` by `scripts/datasheet_stats.py`.",
        "> `scripts/datasheet_stats.py --check` runs in the regeneration gate, so the",
        "> corpus and this document cannot drift apart. Do not edit those sections by",
        "> hand.",
        "",
        "## Composition",
        "",
        f"This release has **{rows:,} rows** "
        f"({stats['by_split']['train']:,} train / {stats['by_split']['eval']:,} eval) "
        f"drawn from {stats['documents']:,} distinct source documents. Its class mix is "
        + " / ".join(
            f"{_ratio(classes[name], rows)} {name.replace('_', '-')}" for name in CLASSES
        )
        + "; the global identical-sentence cap is three.",
        "",
        (
            f"Distinct sentences: **{stats['distinct_sentences']:,} / {rows:,} "
            f"({_ratio(int(stats['distinct_sentences']), rows)})**. The distinct "
            "count is the meaningful corpus size, not the raw row count."
        ),
        "",
        "## Per-source released accounting",
        "",
        "Released rows only — what this repository ships and anyone can count. The",
        "mining funnel that produced them is described under *Mining provenance*,",
        "and is not reproducible from this release.",
        "",
        _row(
            (
                "Source",
                "Released rows",
                "Documents",
                "no_claim",
                *CLAIM_TYPES,
                "Distinct sentences / rows",
            )
        ),
        _row(("---", *(["---:"] * (3 + len(CLAIM_TYPES))), "---:")),
    ]
    for lane in lanes:
        lane_types = lane["types"]
        lines.append(
            _row(
                (
                    lane["label"],
                    f"{lane['rows']:,}",
                    f"{lane['documents']:,}",
                    f"{lane_types['no_claim']:,}",
                    *(f"{lane_types[name]:,}" for name in CLAIM_TYPES),
                    (
                        f"{lane['distinct_sentences']:,} / {lane['rows']:,} "
                        f"({_ratio(int(lane['distinct_sentences']), int(lane['rows']))})"
                    ),
                )
            )
        )

    smallest = min(CLAIM_TYPES, key=lambda name: types[name])
    largest = max(CLAIM_TYPES, key=lambda name: types[name])
    lines += [
        "",
        "## Type coverage",
        "",
        (
            f"**`{smallest}` is the smallest labelled class at {types[smallest]:,} "
            f"pairs**, and `{largest}` the largest at {types[largest]:,}. Every "
            "positive row carries exactly one constraint type; `no_claim` rows are "
            "the negatives and hard-negatives."
        ),
        "",
        _row(("Type", "Released positive pairs", f"Working floor ({WORKING_FLOOR:,})", "Status", "Gap")),
        _row(("---", "---:", "---:", "---", "---:")),
    ]
    for name in CLAIM_TYPES:
        count = types[name]
        met = count >= WORKING_FLOOR
        lines.append(
            _row(
                (
                    name,
                    f"{count:,}",
                    f"{WORKING_FLOOR:,}",
                    "met" if met else "under-sampled",
                    "0" if met else f"{WORKING_FLOOR - count:,}",
                )
            )
        )
    lines += [
        "",
        "No constraint type reaches the working floor. These are counted rows, not",
        "padded targets, and the gap column is the honest distance to a corpus that",
        "would support a per-type claim.",
        "",
        "Lane composition per type:",
        "",
    ]
    for name in CLAIM_TYPES:
        contributors = stats["type_contributors"][name]
        detail = (
            ", ".join(f"{item['rows']:,} from {item['label']}" for item in contributors)
            or "no lane contributed a pair"
        )
        lines.append(f"- `{name}` — {detail}.")

    lines += [
        "",
        "## Filtering, balancing, and split",
        "",
        "Every positive was rechecked with its native-sentence predicate:",
        "dbt/application-code/error-message pairs use the corrected `expressed()`",
        "predicate, SchemaStore uses `expresses_constraint()`, and FHIR uses its",
        "assertion/reference predicates. `accepted_values` labels are reduced to the",
        "enum subset literally stated by the sentence, and numeric expressions",
        "recognise only unambiguous semantic bounds. A sentence that merely names a",
        "field or reports an undifferentiated failure is rejected. No description,",
        "title, adjacent code, or synthetic prose was used to rescue a pair.",
        "",
        "Candidates were read in raw-source order, deduplicated on `(sentence, column,",
        "target)`, and retain the first occurrence's `source_kind`. The cap follows",
        "that deduplication. The split is a deterministic full-document/repository",
        "holdout: `hash(source_kind, source_document) mod 5 == 4` is eval.",
        "",
        (
            "That holdout is checked, not asserted: "
            f"**{len(stats['split_documents_overlap'])} source documents appear in "
            f"both splits**, and eval carries {stats['by_split']['eval']:,} rows "
            f"covering {len(stats['eval_types'])} of {len(CLAIM_TYPES)} constraint "
            f"types ({', '.join(f'`{name}`' for name in stats['eval_types'])})."
        ),
        "",
        "The source data contain negatives and hard-negatives only in the dbt lane.",
        "Cross-corpus negative sampling is therefore not possible without inventing",
        "labels; negatives are instead sampled round-robin across held-out",
        "repository/document lanes, so no individual dbt repository dominates.",
        "Positive sources are all retained.",
        "",
        "## Mining provenance — historical, not reproducible from this release",
        "",
        "The collection pools that these rows were mined from are not shipped in",
        "full. `data/claims/raw/` contains the dbt-lane pool only; the SchemaStore,",
        "FHIR, and licence-gated GitHub-clone pools are not part of this repository.",
        "The funnel figures recorded during collection — rows scanned, positive",
        "candidates, per-source survival rates — are therefore **historical results",
        "of a one-time mining run, and cannot be re-derived from this release**. They",
        "are deliberately absent from the generated tables above rather than restated",
        "as if a reader could check them.",
        "",
        "What can be re-derived is everything the released files contain, which is",
        "what the sections above report.",
        "",
        "## Licence and mining result",
        "",
        "Included sources are dbt repositories (per-row permissive licence),",
        "SchemaStore at its pinned Apache-2.0 commit, FHIR R5 core 5.0.0 under",
        "CC0-1.0, and the licence-gated GitHub clone records (MIT, Apache-2.0, or BSD",
        "as retained per row). Google Discovery remains excluded because payload",
        "redistribution terms were not confirmed. THO, NIEM, and SchemaPile remain",
        "excluded on their recorded copyright/licence grounds. AWS Smithy and Data",
        "Contract CLI supplied no accepted row.",
        "",
        (
            "The error-message and dictionary lane did not substantiate the "
            "expected high-yield result: it contributes "
            f"{next((lane['rows'] for lane in lanes if lane['release'] == 'raw-v6'), 0):,}"
            " released rows, and its repository-discovery records are not training "
            "examples."
        ),
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed datasheet no longer matches the corpus",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the recomputed statistics instead of rendering the document",
    )
    arguments = parser.parse_args(argv)

    stats = measure()
    if unexpected := stats["unexpected_classes"]:
        # A label the datasheet has no column for would otherwise be counted
        # into nothing and silently shrink the published class mix.
        print(f"unrecognised class labels in the corpus: {unexpected}")
        return 1
    if arguments.json:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0

    rendered = _render(stats)
    if arguments.check:
        current = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.exists() else ""
        if current != rendered:
            print(f"{DOCUMENT} is stale; rerun scripts/datasheet_stats.py")
            return 1
        print(f"{DOCUMENT} is current")
        return 0
    DOCUMENT.write_text(rendered, encoding="utf-8")
    print(f"wrote {DOCUMENT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
