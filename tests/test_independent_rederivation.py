"""The engine and the jq re-derivation must reach the same decision, always.

`docs/ENGINE-SPEC.md` states how a verdict resolves, and until now nothing checked
that the shipped engine still does what the document says. `scripts/rederive.jq` is
a second implementation built from that document, sharing no code and no language
with `sidq.policy.engine`. These tests are what make it worth anything: if either
side drifts — the engine away from the spec, or the spec's implementation away from
the engine — a build fails instead of a claim quietly becoming false.

The synthesised cases matter more than the committed ones. Two golden files exercise
a handful of rules; one evidence item per rule exercises all of them, and the
awkward paths (an unhandled kind, a threshold boundary, a comparison Python refuses
to make) are where two implementations of the same prose usually part company.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sidq.models import Evidence
from sidq.policy.engine import PolicyEngine, load_policy
from sidq.serialization import canonical_data

ROOT = Path(__file__).resolve().parents[1]
JQ_PROGRAM = ROOT / "scripts" / "rederive.jq"
POLICY_JSON = ROOT / "src" / "sidq" / "policy" / "default_policy.json"
POLICY_YAML = ROOT / "src" / "sidq" / "policy" / "default_policy.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq is required to run the second implementation"
)


def _rederive(verdict_payload: Any) -> dict[str, Any]:
    """Run the jq implementation over a verdict-shaped document."""
    result = subprocess.run(
        [
            "jq",
            "--argjson",
            "policy",
            POLICY_JSON.read_text(encoding="utf-8"),
            "-f",
            str(JQ_PROGRAM),
        ],
        input=json.dumps(verdict_payload),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"jq failed: {result.stderr}"
    return json.loads(result.stdout)


def _decide_both(evidence: list[Evidence]) -> tuple[dict[str, Any], dict[str, Any]]:
    verdict = PolicyEngine(None).decide(evidence)
    payload = canonical_data(verdict)
    return (
        {"decision": verdict.decision, "reason_code": verdict.reason_code},
        {
            "decision": _rederive(payload)["decision"],
            "reason_code": _rederive(payload)["reason_code"],
        },
    )


def test_the_json_rendering_of_the_policy_is_current() -> None:
    """jq cannot read YAML, so the JSON is generated — and must never drift."""
    generated = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "policy_to_json.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert generated.returncode == 0, generated.stderr or generated.stdout


def test_the_generated_json_is_a_faithful_rendering_of_the_pinned_policy() -> None:
    """The YAML is what `policy_hash` pins; the JSON must say the same thing."""
    import yaml

    assert json.loads(POLICY_JSON.read_text(encoding="utf-8")) == yaml.safe_load(
        POLICY_YAML.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "verdict_path",
    sorted(ROOT.glob("examples/*/verdict.json")),
    ids=lambda path: path.parent.name,
)
def test_jq_rederives_every_committed_verdict(verdict_path: Path) -> None:
    published = json.loads(verdict_path.read_text(encoding="utf-8"))
    derived = _rederive(published)
    assert derived["decision"] == published["decision"]
    assert derived["reason_code"] == published.get("reason_code")


@pytest.mark.parametrize("rule_id", [rule.id for rule in load_policy().rules])
def test_the_two_implementations_agree_on_every_rule(rule_id: str) -> None:
    """One evidence item per rule, so no rule ships unchecked by the second reader."""
    rule = next(item for item in load_policy().rules if item.id == rule_id)
    detail: dict[str, Any] = {
        "critical_assets": ["urn:li:dataset:(x,y,PROD)"],
        "cross_team_owners": ["urn:li:corpuser:someone"],
        "unreadable_assets": ["urn:li:dataset:(x,z,PROD)"],
        "downstream_count": 99,
    }
    engine, jq = _decide_both(
        [Evidence(rule.evidence_kind, "urn:li:dataset:(a,b,PROD)", detail)]
    )
    assert engine == jq


def test_the_two_implementations_agree_on_an_unhandled_kind() -> None:
    """`unhandled_evidence: block` is the fail-closed contract; check it twice."""
    engine, jq = _decide_both(
        [Evidence("a_kind_no_rule_handles", "urn:li:dataset:(a,b,PROD)", {})]
    )
    assert engine == jq
    assert engine["decision"] == "BLOCK"


@pytest.mark.parametrize("downstream_count", [4, 5, 6])
def test_the_two_implementations_agree_at_the_threshold(downstream_count: int) -> None:
    """`wide_blast_radius` is `gt` a setting — the boundary is where readers differ."""
    engine, jq = _decide_both(
        [
            Evidence(
                "blast_radius",
                "urn:li:dataset:(a,b,PROD)",
                {"downstream_count": downstream_count},
            )
        ]
    )
    assert engine == jq


def test_the_two_implementations_agree_on_a_comparison_python_refuses() -> None:
    """Python raises on `5 > "five"` and the engine blocks; jq orders everything.

    Without matching that refusal the second implementation would answer where the
    engine declines, and the disagreement would be invisible until a real catalog
    produced an ill-typed detail.
    """
    engine, jq = _decide_both(
        [
            Evidence(
                "blast_radius",
                "urn:li:dataset:(a,b,PROD)",
                {"downstream_count": "many"},
            )
        ]
    )
    assert engine == jq
    assert engine["decision"] == "BLOCK"
