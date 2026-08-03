"""The published datasheet must equal the corpus it describes.

This is the one document whose entire value is that a reader can check it, and
it had drifted: a remined SchemaStore lane moved from 293 released rows to 287,
and the class mix, distinct-sentence ratio, `accepted_values` total, and
per-source table all kept quoting the old funnel. Every one of those is
falsifiable with `wc -l` and a `Counter` — which is exactly why a stale number
there costs more than a stale number anywhere else in the repo.

The numbers are generated now. These tests hold that generation honest from
both directions: the committed document must match a fresh render, and the
generated statistics must match the released files counted independently here,
so a bug in the generator cannot quietly agree with itself.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from scripts.datasheet_stats import CLAIM_TYPES, DOCUMENT, main, measure

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "claims"


def _released_rows() -> list[dict[str, Any]]:
    """Read the corpus without going through the generator's own helpers."""
    rows: list[dict[str, Any]] = []
    for split in ("train", "eval"):
        for line in (
            (CORPUS / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
        ):
            if line.strip():
                row = json.loads(line)
                row["_split"] = split
                rows.append(row)
    return rows


def test_the_committed_datasheet_matches_the_released_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The regeneration gate, as a test: edit the corpus, this fails."""
    assert main(["--check"]) == 0, capsys.readouterr().out


def test_the_generated_totals_match_an_independent_count() -> None:
    stats = measure()
    rows = _released_rows()

    assert stats["rows"] == len(rows)
    assert stats["by_split"] == {
        "train": sum(1 for row in rows if row["_split"] == "train"),
        "eval": sum(1 for row in rows if row["_split"] == "eval"),
    }
    assert sum(stats["classes"].values()) == len(rows)
    assert not stats["unexpected_classes"]
    assert stats["distinct_sentences"] == len(
        {row["input"]["sentence"] for row in rows}
    )


def test_every_lane_row_is_accounted_for_exactly_once() -> None:
    """Nothing may vanish between the corpus and the per-source table."""
    stats = measure()

    assert sum(int(lane["rows"]) for lane in stats["lanes"]) == stats["rows"]
    for lane in stats["lanes"]:
        assert sum(lane["types"].values()) == lane["rows"], lane["label"]
        assert lane["distinct_sentences"] <= lane["rows"]


def test_the_type_table_counts_positives_and_only_positives() -> None:
    rows = _released_rows()
    stats = measure()

    expected = Counter(
        row["target"]["claim"]["type"] for row in rows if row["class"] == "positive"
    )
    assert stats["types"] == {name: expected.get(name, 0) for name in CLAIM_TYPES}
    assert sum(stats["types"].values()) == sum(
        1 for row in rows if row["class"] == "positive"
    )
    # A negative carrying a constraint type would be a labelling bug that the
    # type table would otherwise absorb without complaint.
    assert all(
        row["target"]["claim"] is None for row in rows if row["class"] != "positive"
    )


def test_the_document_holdout_actually_holds() -> None:
    """The split's whole claim is that no source document spans both files."""
    stats = measure()
    rows = _released_rows()

    train = {row["source_document"] for row in rows if row["_split"] == "train"}
    evaluation = {row["source_document"] for row in rows if row["_split"] == "eval"}

    assert stats["split_documents_overlap"] == []
    assert not train & evaluation


def test_the_datasheet_claims_no_artifact_the_release_does_not_contain() -> None:
    """It referenced `refilter-v6-report.json`, which is not in this repository.

    A datasheet that cites its own evidence file has to ship it. Naming a file a
    reader cannot open is the same failure class as an unverifiable number, and
    it is easier to reintroduce, so it is pinned rather than remembered.
    """
    text = DOCUMENT.read_text(encoding="utf-8")

    for token in text.split("`"):
        candidate = token.strip()
        if not candidate.endswith((".json", ".jsonl", ".npz")):
            continue
        matches = list(CORPUS.rglob(candidate)) or list(ROOT.rglob(candidate))
        assert matches, (
            f"the datasheet names {candidate}, which this release does not ship"
        )


def test_the_datasheet_does_not_claim_reproducible_mining() -> None:
    """Survival rates came from pools this release does not ship — say so."""
    text = DOCUMENT.read_text(encoding="utf-8")

    assert "historical" in text.lower()
    assert "not reproducible from this release" in text.lower()
    # The funnel columns are gone from the generated table; if they return, they
    # must return with the pools that make them checkable.
    assert "Positive candidates" not in text
    assert "Survival" not in text
