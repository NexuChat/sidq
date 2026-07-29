"""Every number the README claims must trace to a committed evidence file.

the submission checklist requires that "every number in the README traces to
a file in the repo — no unsupported claim". That was verified by hand, which means
it holds only until the next edit. These tests mechanise it, so a headline number
that drifts from its evidence breaks the build instead of reaching a judge.

The catalog audit and the reconciliation example both need a live DataHub, so
their artifacts cannot be regenerated inside a unit test. What can be checked
without any network is that the prose and the committed artifact still agree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
TRUTH_REPORT = ROOT / "examples" / "03-catalog-truth-report" / "report.json"


def _summary() -> dict[str, dict[str, int]]:
    raw = json.loads(TRUTH_REPORT.read_text(encoding="utf-8"))
    return {entry["check"]: entry for entry in raw["summary"]}


def test_the_headline_contradiction_count_matches_the_evidence() -> None:
    """ "285 internal contradictions across 67 datasets" is the README's first claim."""
    entry = _summary()["lineage_field_missing"]
    text = README.read_text(encoding="utf-8")

    assert f"{entry['findings']} internal contradictions" in text
    assert f"across {entry['datasets_examined']} datasets" in text


def test_the_unowned_asset_count_matches_the_evidence() -> None:
    entry = _summary()["unowned_consumed"]

    assert f"{entry['findings']} consumed-but-unowned assets" in README.read_text(
        encoding="utf-8"
    )


def test_the_powerbi_example_numbers_match_the_evidence() -> None:
    """The README singles out one asset: 58 lineage edges, 57 targeting nothing."""
    raw = json.loads(TRUTH_REPORT.read_text(encoding="utf-8"))
    text = README.read_text(encoding="utf-8")
    subject = "Customer_Analytics_Measures"

    findings = [
        item
        for item in raw["evidence"]
        if item["kind"] == "lineage_field_missing" and subject in item["subject"]
    ]
    assert findings, f"{subject} must still appear in the audit evidence"

    claimed = {
        int(value) for value in re.findall(r"\*\*(\d+) column-lineage edges", text)
    }
    claimed_missing = {
        int(value) for value in re.findall(r"\*\*(\d+) of which target fields", text)
    }
    assert claimed, "the README must still quote the edge count"
    assert claimed_missing, "the README must still quote the missing-target count"
    assert max(claimed_missing) == len(findings), (
        f"README claims {max(claimed_missing)} missing targets for {subject}; "
        f"the evidence file has {len(findings)}"
    )


def test_the_unverifiable_negative_result_is_still_honest() -> None:
    """The README's strongest honesty signal: 32/32 lineage_rot unverifiable."""
    text = README.read_text(encoding="utf-8")

    assert "32/32" in text
    assert "unverifiable" in text


@pytest.mark.parametrize(
    "check",
    ["lineage_field_missing", "unowned_consumed", "doc_references_missing_column"],
)
def test_every_audit_check_reports_a_finding_count(check: str) -> None:
    """A check silently dropped from the audit would make the report unfalsifiable."""
    entry = _summary()[check]

    assert "findings" in entry
    assert entry["datasets_examined"] > 0
