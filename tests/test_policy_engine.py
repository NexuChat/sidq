from __future__ import annotations

import hashlib
from importlib.resources import files

import pytest

from sidq.models import Evidence
from sidq.policy.engine import PolicyConfigError, PolicyEngine, load_policy

EXPLICIT_INFORMATIONAL_KINDS = {
    "constraint_confirmed",
    "constraint_missing_in_catalog",
    "constraint_unverifiable",
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
            "doc_claim_holds",
            None,
        ),
        (
            Evidence("lineage_unverifiable", "urn:orders#total", {}),
            "PASS",
            "lineage_unverifiable",
            None,
        ),
        (
            Evidence("unowned_consumed", "urn:unrelated", {}),
            "PASS",
            "unowned_consumed",
            None,
        ),
        (
            Evidence("pii_leak_untagged_unverifiable", "catalog", {}),
            "PASS",
            "pii_leak_untagged_unverifiable",
            None,
        ),
        (
            Evidence("future_evidence", "urn:future", {"fact": True}),
            "BLOCK",
            "unhandled_evidence",
            "UNVERIFIABLE_CHANGE",
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
    if decision == "PASS":
        assert verdict.findings[0].severity == "info"
        assert verdict.findings[0].evidence == (evidence,)


@pytest.mark.parametrize(
    ("downstream_count", "decision", "rule_id"),
    [(5, "PASS", "informational"), (6, "WARN", "wide_blast_radius")],
)
def test_blast_radius_setting_has_an_exact_boundary(
    downstream_count: int, decision: str, rule_id: str
) -> None:
    evidence = Evidence(
        "blast_radius",
        "urn:orders",
        {
            "critical_assets": [],
            "cross_team_owners": [],
            "unreadable_assets": [],
            "downstream_count": downstream_count,
        },
    )

    verdict = PolicyEngine().decide([evidence])

    assert verdict.decision == decision
    assert [finding.rule_id for finding in verdict.findings] == [rule_id]


def test_where_any_blocks_when_either_risk_signal_is_present() -> None:
    base = {
        "critical_assets": [],
        "cross_team_owners": [],
        "unreadable_assets": [],
        "downstream_count": 0,
    }

    critical = PolicyEngine().decide(
        [Evidence("blast_radius", "urn:orders", {**base, "critical_assets": ["x"]})]
    )
    cross_team = PolicyEngine().decide(
        [
            Evidence(
                "blast_radius",
                "urn:orders",
                {**base, "cross_team_owners": ["finance"]},
            )
        ]
    )

    assert critical.decision == "BLOCK"
    assert cross_team.decision == "BLOCK"
    assert critical.findings[0].rule_id == "critical_downstream"
    assert cross_team.findings[0].rule_id == "critical_downstream"


def test_invalid_numeric_evidence_cannot_bypass_a_threshold() -> None:
    evidence = Evidence(
        "blast_radius",
        "urn:orders",
        {
            "critical_assets": [],
            "cross_team_owners": [],
            "unreadable_assets": [],
            "downstream_count": "six",
        },
    )

    verdict = PolicyEngine().decide([evidence])

    assert verdict.decision == "BLOCK"
    assert verdict.reason_code == "UNVERIFIABLE_CHANGE"
    assert verdict.findings[0].rule_id == "policy_evaluation_failed"


@pytest.mark.parametrize(
    "body",
    [
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: subject\n          op: eval\n          value: x\n    severity: block\n    message: bad\n""",
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: unknown\n          op: eq\n          value: x\n    severity: block\n    message: bad\n""",
        """version: 1\nrules:\n  - id: bad\n    match:\n      evidence_kind: x\n      where:\n        - field: subject\n          op: eq\n          value: $settings.missing\n    severity: block\n    message: bad\n""",
        """version: 1\nunhandled_evidence: pass\nrules: []\n""",
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


def test_default_policy_is_a_package_resource() -> None:
    resource = files("sidq.policy").joinpath("default_policy.yaml")

    assert resource.is_file()
    assert (
        load_policy().policy_hash == hashlib.sha256(resource.read_bytes()).hexdigest()
    )


def test_informational_evidence_is_an_explicit_policy_contract() -> None:
    info_rules = {
        rule.evidence_kind: rule
        for rule in load_policy().rules
        if rule.severity == "info"
    }

    assert set(info_rules) == EXPLICIT_INFORMATIONAL_KINDS
    for kind in EXPLICIT_INFORMATIONAL_KINDS:
        finding = PolicyEngine().decide([Evidence(kind, "catalog", {})]).findings[0]
        assert finding.rule_id == kind
        assert finding.severity == "info"


def test_custom_policy_can_explicitly_classify_evidence_as_info(tmp_path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """version: 1
rules:
  - id: observed
    match:
      evidence_kind: observed
    severity: info
    message: Observation for {subject}.
""",
        encoding="utf-8",
    )

    verdict = PolicyEngine(policy_file).decide([Evidence("observed", "asset", {})])

    assert verdict.decision == "PASS"
    assert verdict.findings[0].severity == "info"


def test_legacy_custom_policy_can_still_intentionally_relax_all_rules(tmp_path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("version: 1\nrules: []\n", encoding="utf-8")

    verdict = PolicyEngine(policy_file).decide([Evidence("observed", "asset", {})])

    assert verdict.decision == "PASS"
    assert verdict.findings[0].rule_id == "informational"


def test_custom_policy_resolves_settings_in_conditions(tmp_path) -> None:
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        """version: 1
settings:
  warning_threshold: 10
rules:
  - id: high_count
    match:
      evidence_kind: count
      where:
        - field: detail.value
          op: gte
          value: $settings.warning_threshold
    severity: warn
    message: High count for {subject}.
""",
        encoding="utf-8",
    )
    engine = PolicyEngine(policy_file)

    below = engine.decide([Evidence("count", "asset", {"value": 9})])
    boundary = engine.decide([Evidence("count", "asset", {"value": 10})])

    assert below.decision == "PASS"
    assert below.findings[0].rule_id == "informational"
    assert boundary.decision == "WARN"
    assert boundary.findings[0].rule_id == "high_count"
