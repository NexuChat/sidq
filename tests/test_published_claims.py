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
from typing import get_args

import pytest

from sidq.mcp_server.server import UnverifiableResult

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


# ---------------------------------------------------------------------------
# The documented tool contract must not fall behind the shipped tool.
# ---------------------------------------------------------------------------

CHECK_NAMES = frozenset(get_args(UnverifiableResult.model_fields["check"].annotation))
CONTRACT_DOCS = (
    "docs/MCP-SERVER.md",
    "skills/datahub-verify/SKILL.md",
)


def test_the_check_names_are_the_ones_the_tool_can_actually_report() -> None:
    """Pin the set so the guard below cannot be weakened by accident."""
    assert CHECK_NAMES == {
        "schema_drift",
        "lineage_rot",
        "constraint_reconciliation",
    }


@pytest.mark.parametrize("document", CONTRACT_DOCS)
def test_every_documented_contract_names_every_check(document: str) -> None:
    """A contract that omits a check teaches an agent an incomplete tool.

    `constraint_reconciliation` shipped in `verify_context` while these documents
    still described two checks, and `docs/MCP-SERVER.md` stated outright that
    `truthful` depends on "both checks". `skills/datahub-verify/SKILL.md` is worse
    than an internal doc being wrong: it is the upstream contribution, so it would
    have taught every agent that installs it a tool narrower than the one it calls.
    """
    text = (ROOT / document).read_text(encoding="utf-8")
    missing = sorted(name for name in CHECK_NAMES if name not in text)

    assert not missing, f"{document} does not document: {', '.join(missing)}"


@pytest.mark.parametrize("document", ("README.md", "docs/DEVPOST.md"))
def test_the_judge_facing_summaries_mention_reconciliation(document: str) -> None:
    """The judge-facing copy must describe what the engine actually does."""
    text = (ROOT / document).read_text(encoding="utf-8").lower()

    assert "reconcil" in text, (
        f"{document} does not mention constraint reconciliation, which ships in "
        "verify_context and is the strongest form of the catalog-truth thesis"
    )


# ---------------------------------------------------------------------------
# The landing page. It is the first surface a judge opens, it hardcodes values
# from the generated verdict, and it claims to show "Real engine output".
# ---------------------------------------------------------------------------

LANDING = ROOT / "web" / "index.html"


def _blast_detail() -> dict:
    verdict = json.loads(
        (ROOT / "examples" / "01-blocked-pii-dashboard" / "verdict.json").read_text(
            encoding="utf-8"
        )
    )
    return next(
        evidence["detail"]
        for finding in verdict["findings"]
        for evidence in finding["evidence"]
        if evidence["kind"] == "blast_radius"
    )


def test_the_landing_page_decision_and_commit_match_the_verdict() -> None:
    verdict = json.loads(
        (ROOT / "examples" / "01-blocked-pii-dashboard" / "verdict.json").read_text(
            encoding="utf-8"
        )
    )
    html = LANDING.read_text(encoding="utf-8")

    assert f'id="verdict-title">{verdict["decision"]}<' in html
    assert verdict["commit_sha"] in html
    assert any(finding["rule_id"] in html for finding in verdict["findings"])


def test_the_landing_page_node_count_matches_the_proven_lineage() -> None:
    """The page advertises a node count; it must be the chain the verdict proves."""
    path = _blast_detail()["paths"][0]
    hops = path["hops"]
    nodes = []
    for key in sorted(hops):
        if not nodes:
            nodes.append(hops[key]["from"])
        nodes.append(hops[key]["to"])
    distinct = len(dict.fromkeys(nodes))

    assert f"{distinct:02d} nodes" in LANDING.read_text(encoding="utf-8"), (
        f"the landing page must advertise {distinct} lineage nodes"
    )


def test_the_landing_page_names_the_dashboard_the_verdict_reaches() -> None:
    dashboards = _blast_detail()["dashboards"]
    html = LANDING.read_text(encoding="utf-8")

    assert dashboards, "the blocked example must still reach a dashboard"
    for urn in dashboards:
        identifier = urn.rsplit(".", 1)[-1].rstrip(")")
        assert f"dashboards.{identifier}" in html


def test_the_landing_page_does_not_point_a_judge_at_localhost() -> None:
    """A dead link here burns the strongest thirty seconds of the submission."""
    html = LANDING.read_text(encoding="utf-8")

    assert "localhost" not in html
