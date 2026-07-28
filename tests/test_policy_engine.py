from __future__ import annotations

import hashlib

import pytest

from sidq.models import Evidence
from sidq.policy.engine import PolicyConfigError, PolicyEngine, load_policy


@pytest.mark.parametrize(
    ("evidence", "decision", "rule_id", "reason_code"),
    [
        (Evidence("catalog_reality_mismatch", "urn:catalog", {"graph_fields": ["old"], "live_fields": ["new"]}), "BLOCK", "catalog_reality_mismatch", "STALE_CONTEXT"),
        (Evidence("unknown_field", "urn:orders#missing", {}), "BLOCK", "unknown_field", None),
        (Evidence("pii_exposure", "urn:customers#email", {}), "BLOCK", "pii_exposure", None),
        (Evidence("assertion_dependency_break", "urn:orders#total", {}), "BLOCK", "assertion_dependency_break", None),
        (Evidence("blast_radius", "urn:orders", {"critical_assets": ["urn:dashboard"]}), "BLOCK", "critical_downstream", None),
        (Evidence("blast_radius", "urn:orders", {"cross_team_owners": ["finance"], "critical_assets": []}), "BLOCK", "critical_downstream", None),
        (Evidence("blast_radius", "urn:orders", {"downstream_count": 6, "critical_assets": []}), "WARN", "wide_blast_radius", None),
        (Evidence("deprecated_upstream", "urn:legacy", {}), "WARN", "deprecated_upstream", None),
        (Evidence("unowned_asset", "urn:orphan", {}), "WARN", "unowned_asset", None),
        (Evidence("future_evidence", "urn:future", {"fact": True}), "PASS", "informational", None),
    ],
)
def test_default_policy_table(evidence: Evidence, decision: str, rule_id: str, reason_code: str | None) -> None:
    verdict = PolicyEngine().decide([evidence])

    assert verdict.decision == decision
    assert verdict.reason_code == reason_code
    assert [finding.rule_id for finding in verdict.findings] == [rule_id]
    if rule_id == "informational":
        assert verdict.findings[0].severity == "info"
        assert verdict.findings[0].evidence == (evidence,)


@pytest.mark.parametrize(
    "body",
    [
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: subject\n          op: eval\n          value: x\n    severity: block\n    message: bad\n""",
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: unknown\n          op: eq\n          value: x\n    severity: block\n    message: bad\n""",
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: subject\n          op: eq\n          value: $settings.missing\n    severity: block\n    message: bad\n""",
    ],
)
def test_invalid_rules_fail_when_loaded(tmp_path, body: str) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(body, encoding="utf-8")

    with pytest.raises(PolicyConfigError):
        load_policy(policy_file)


def test_policy_hash_uses_actual_policy_bytes(tmp_path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """version: 1\nrules: []\n""",
        encoding="utf-8",
    )

    assert load_policy(policy_file).policy_hash == hashlib.sha256(policy_file.read_bytes()).hexdigest()
