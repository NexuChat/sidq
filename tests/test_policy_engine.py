from __future__ import annotations

import hashlib

import pytest

from sidq.models import Evidence
from sidq.policy.engine import PolicyConfigError, PolicyEngine, load_policy

EMITTED_EVIDENCE_KINDS = {
    "access_policy_conflict",
    "blast_radius",
    "catalog_reality_mismatch",
    "deprecated_upstream",
    "deprecated_upstream_of_live",
    "deprecated_upstream_of_live_unverifiable",
    "doc_claim_holds",
    "doc_claim_unverifiable",
    "doc_claim_violated",
    "doc_references_missing_column",
    "doc_references_missing_column_unverifiable",
    "doc_rot",
    "graph_unavailable",
    "lineage_field_missing",
    "lineage_field_missing_unverifiable",
    "lineage_rot_extra",
    "lineage_rot_missing",
    "lineage_unverifiable",
    "orphan_lineage",
    "orphan_lineage_unverifiable",
    "pii_exposure",
    "pii_leak_untagged",
    "pii_leak_untagged_unverifiable",
    "type_mismatch",
    "unknown_dataset",
    "unknown_field",
    "unowned_asset",
    "unowned_consumed",
    "unowned_consumed_unverifiable",
    "unparseable_sql",
    "unresolved_asset",
}
INFORMATIONAL_CATALOG_HEALTH_KINDS = {
    "deprecated_upstream_of_live_unverifiable",
    "doc_claim_holds",
    "doc_references_missing_column_unverifiable",
    "lineage_field_missing_unverifiable",
    "lineage_unverifiable",
    "orphan_lineage_unverifiable",
    "pii_leak_untagged_unverifiable",
    "unowned_consumed",
    "unowned_consumed_unverifiable",
}


@pytest.mark.parametrize(
    ("evidence", "decision", "rule_id", "reason_code"),
    [
        (
            Evidence(
                "catalog_reality_mismatch",
                "urn:catalog",
                {"graph_fields": ["old"], "live_fields": ["new"]},
            ),
            "BLOCK",
            "catalog_reality_mismatch",
            "STALE_CONTEXT",
        ),
        (
            Evidence("unresolved_asset", "notes/plan.txt", {}),
            "BLOCK",
            "unresolved_asset",
            "UNVERIFIABLE_CHANGE",
        ),
        (
            Evidence("unparseable_sql", "urn:orders", {}),
            "BLOCK",
            "unparseable_sql",
            "UNVERIFIABLE_CHANGE",
        ),
        (
            Evidence("graph_unavailable", "urn:orders", {}),
            "BLOCK",
            "graph_unavailable",
            "UNVERIFIABLE_CHANGE",
        ),
        (
            Evidence("unknown_field", "urn:orders#missing", {}),
            "BLOCK",
            "unknown_field",
            None,
        ),
        (
            Evidence("unknown_dataset", "urn:missing", {}),
            "BLOCK",
            "unknown_dataset",
            "UNVERIFIABLE_CHANGE",
        ),
        (
            Evidence("type_mismatch", "urn:orders#total", {}),
            "BLOCK",
            "type_mismatch",
            None,
        ),
        (
            Evidence("pii_exposure", "urn:customers#email", {}),
            "BLOCK",
            "pii_exposure",
            None,
        ),
        (
            Evidence("assertion_dependency_break", "urn:orders#total", {}),
            "BLOCK",
            "assertion_dependency_break",
            None,
        ),
        (
            Evidence(
                "blast_radius", "urn:orders", {"critical_assets": ["urn:dashboard"]}
            ),
            "BLOCK",
            "critical_downstream",
            None,
        ),
        (
            Evidence(
                "blast_radius",
                "urn:orders",
                {"cross_team_owners": ["finance"], "critical_assets": []},
            ),
            "BLOCK",
            "critical_downstream",
            None,
        ),
        (
            Evidence(
                "blast_radius",
                "urn:orders",
                {"downstream_count": 6, "critical_assets": []},
            ),
            "WARN",
            "wide_blast_radius",
            None,
        ),
        (
            Evidence("deprecated_upstream", "urn:legacy", {}),
            "WARN",
            "deprecated_upstream",
            None,
        ),
        (Evidence("unowned_asset", "urn:orphan", {}), "WARN", "unowned_asset", None),
        (
            Evidence("doc_claim_unverifiable", "urn:orders#total", {}),
            "BLOCK",
            "doc_claim_unverifiable",
            "UNVERIFIABLE_CHANGE",
        ),
        (
            Evidence("doc_claim_holds", "urn:orders#total", {}),
            "PASS",
            "informational",
            None,
        ),
        (
            Evidence("lineage_unverifiable", "urn:orders#total", {}),
            "PASS",
            "informational",
            None,
        ),
        (
            Evidence("unowned_consumed", "urn:unrelated", {}),
            "PASS",
            "informational",
            None,
        ),
        (
            Evidence("pii_leak_untagged_unverifiable", "catalog", {}),
            "PASS",
            "informational",
            None,
        ),
        (
            Evidence("future_evidence", "urn:future", {"fact": True}),
            "PASS",
            "informational",
            None,
        ),
    ],
)
def test_default_policy_table(
    evidence: Evidence, decision: str, rule_id: str, reason_code: str | None
) -> None:
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

    assert (
        load_policy(policy_file).policy_hash
        == hashlib.sha256(policy_file.read_bytes()).hexdigest()
    )


def test_every_emitted_evidence_kind_is_ruled_or_an_explicit_info_exception() -> None:
    policy_kinds = {rule.evidence_kind for rule in load_policy().rules}

    assert EMITTED_EVIDENCE_KINDS - INFORMATIONAL_CATALOG_HEALTH_KINDS <= policy_kinds
    for kind in INFORMATIONAL_CATALOG_HEALTH_KINDS:
        finding = PolicyEngine().decide([Evidence(kind, "catalog", {})]).findings[0]
        assert finding.rule_id == "informational"
        assert finding.severity == "info"
