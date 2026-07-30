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

from scripts import eval_preflight
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


# ---------------------------------------------------------------------------
# The generated truth report, and the video script the owner narrates on camera.
# ---------------------------------------------------------------------------

TRUTH_DOC = ROOT / "docs" / "TRUTH-REPORT.md"
VIDEO = ROOT / "docs" / "the video plan"


def test_the_generated_truth_report_agrees_with_its_own_evidence() -> None:
    """`docs/TRUTH-REPORT.md` and `report.json` come from one script run.

    The markdown is generated beside the JSON, so the two can only disagree if one
    was edited by hand — which is precisely how the published `policy_hash` drifted.
    """
    summary = _summary()
    text = TRUTH_DOC.read_text(encoding="utf-8")

    for check, entry in summary.items():
        row = next(
            (line for line in text.splitlines() if line.startswith(f"| `{check}`")),
            None,
        )
        assert row is not None, f"{check} is missing from the published report table"
        assert str(entry["findings"]) in row, (
            f"{check} reports {entry['findings']} findings; the table row says {row!r}"
        )


def test_every_contradiction_count_the_video_narrates_is_the_real_one() -> None:
    """The owner reads these aloud on camera; a stale figure cannot be corrected later.

    The script chooses which counts to narrate — it states the contradiction and
    dataset totals and skips the unowned-asset one — so this checks the numbers it
    actually claims rather than demanding it recite every metric. Unlike every other
    artifact here, a wrong number is unfixable once recorded.
    """
    entry = _summary()["lineage_field_missing"]
    text = VIDEO.read_text(encoding="utf-8")

    narrated = {
        int(value)
        for value in re.findall(r"(\d+)\s+internal field-lineage contradictions", text)
    }
    assert narrated, "the video script must state the contradiction count it shows"
    assert narrated == {entry["findings"]}, (
        f"the script narrates {narrated}; the evidence says {entry['findings']}"
    )

    datasets = {int(value) for value in re.findall(r"across (\d+) datasets", text)}
    assert datasets == {entry["datasets_examined"]}, (
        f"the script narrates {datasets} datasets; "
        f"the evidence says {entry['datasets_examined']}"
    )


def test_the_video_script_keeps_the_negative_result_it_promises_to_state() -> None:
    """The script commits to saying the unverifiable count out loud. Hold it to that."""
    text = VIDEO.read_text(encoding="utf-8")

    assert "32" in text
    assert "unverifiable" in text.lower()
    # The script also lists claims it must NOT make; that discipline is the point.
    assert "A claim that the 285 contradictions prove" in text, (
        "the video script must keep its explicit list of claims not to make"
    )


# ---------------------------------------------------------------------------
# The pre-flight negative result. PREFLIGHT.md §6 pre-commits to publishing it.
# ---------------------------------------------------------------------------


def test_the_preflight_result_matches_the_corpus_it_reports_on() -> None:
    """A published negative result is still a claim, and must stay true."""
    assert eval_preflight.render(
        eval_preflight.evaluate(eval_preflight.load())
    ) == eval_preflight.DOCUMENT.read_text(encoding="utf-8"), (
        "docs/PREFLIGHT-RESULTS.md is stale; rerun scripts/eval_preflight.py"
    )


def test_the_corpus_input_contract_has_no_verdict_leak() -> None:
    """§3's leak rule, enforced mechanically rather than by reviewer vigilance.

    A feature built from the verdict would score beautifully and be worthless —
    the failure mode §3 calls hardest to notice after the fact.
    """
    result = eval_preflight.evaluate(eval_preflight.load())

    assert result["leaked_keys"] == []
    assert result["unexpected_keys"] == []


def test_preflight_is_not_advertised_as_shipped_while_it_is_not() -> None:
    """The spec is a binding contract; it must not read as delivered capability."""
    spec = (ROOT / "docs" / "PREFLIGHT.md").read_text(encoding="utf-8")
    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")

    assert "not shipped" in spec.lower()
    assert "not shipped" in results.lower()


def test_a_non_model_rung_still_ties_the_best_model() -> None:
    """The published conclusion rests on this, so it is asserted, not narrated.

    This assertion has been rewritten three times, each time because it fired when
    the facts moved: when the corpus gained label variance, when deterministic
    pre-checks took the false-negative rate under the bar, and when a two-term rule
    turned out to tie the classifiers. That is the guard doing its job — a
    published conclusion has to keep matching its own evidence.
    """
    rungs = eval_preflight.load_rungs()
    assert rungs is not None, "run scripts/train_preflight.py"
    by_name = {rung["rung"]: rung for rung in rungs["rungs"]}

    rule = next(rung for name, rung in by_name.items() if name.startswith("L0.75"))
    models = [
        rung
        for name, rung in by_name.items()
        if name.startswith(("L1", "L2")) and rung["abstention_rate"] <= 0.5
    ]
    assert models, "the ladder must publish trained rungs to compare against"
    best_model = min(models, key=lambda rung: rung["false_negative_rate"])

    assert rule["false_negative_rate"] <= best_model["false_negative_rate"], (
        "a trained rung now beats the deterministic rule; §1's first condition may "
        "hold after all and both documents must be rewritten before the "
        "no-model-needed conclusion can stand"
    )
    assert rule["false_positives"] <= best_model["false_positives"]

    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")
    assert "no model is needed" in results
    assert "cannot tell us whether pre-flight is hard" in results


def test_the_published_deliverable_is_the_cheapest_rung_that_meets_the_bar() -> None:
    """§4: cheapest, not most sophisticated. A tie must resolve downward."""
    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")

    assert "The cheapest rung that meets the bar is **L0.75" in results, (
        "the report must name the rule, not a classifier that merely ties it"
    )


def test_the_ladder_was_split_by_model_not_by_row() -> None:
    """§3: a row-wise split measures memorisation and reports a useless number."""
    rungs = eval_preflight.load_rungs()
    assert rungs is not None

    assert rungs["holdout_models"], "the held-out models must be named"
    assert rungs["train_rows"] and rungs["test_rows"]
    # Every held-out name is a dbt model path, not a row id.
    assert all(name.endswith(".sql") for name in rungs["holdout_models"])


def test_the_readme_audit_section_agrees_with_the_published_evidence() -> None:
    """The one-command pitch must quote the guarded number, not a live-run one.

    The README now describes a live `sidq audit` run beside the recorded audit,
    and the two legitimately differ — the live catalog carries the demo project as
    well as the sample. The contradiction count is identical in both and is the
    figure the pitch leans on, so that one is pinned here; the unowned count is
    explained in prose rather than quoted as a headline, because a number that
    moves with catalog contents cannot be a claim.
    """
    entry = _summary()["lineage_field_missing"]
    text = README.read_text(encoding="utf-8")

    assert f"all {entry['findings']} `lineage_field_missing` contradictions" in text
    # The scope difference must stay explained, not quietly dropped.
    assert "Same check, different catalog contents." in text


def test_the_readme_leads_with_something_a_judge_can_run() -> None:
    """The first actionable thing must be a command, not an installation."""
    text = README.read_text(encoding="utf-8")

    assert "## Try it in one command" in text
    assert "sidq audit" in text
    # Writing to someone else's catalog must never read as the default.
    assert "It is off by\ndefault" in text or "off by default" in text
