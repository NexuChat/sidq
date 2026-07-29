from __future__ import annotations

from pathlib import Path

from sidq.gates.governance import GovernanceGate
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import Evidence, FieldRef, TouchedAsset
from sidq.policy.engine import PolicyEngine

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
LEGACY = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.legacy_customers,PROD)"
RESTRICTED = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.restricted_customers,PROD)"
PUBLIC = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.public_customers,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


class ReplayGovernanceGraph(ReplayGraphClient):
    """Replay real graph calls, with explicit metadata for each isolated case."""

    def __init__(self, **datasets: DatasetInfo) -> None:
        super().__init__(FIXTURES)
        self.datasets = datasets

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        if urn in self.datasets:
            return self.datasets[urn]
        return super().get_dataset(urn)

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        if urn == RESTRICTED:
            return LineageResult(
                urns=(PUBLIC,),
                columns={PUBLIC: (column or "email",)},
                tags={PUBLIC: ()},
                granularity="column",
            )
        return super().get_downstream(urn, depth, column)


def _asset(
    urn: str = CUSTOMERS,
    *,
    removed: tuple[str, ...] = (),
    references: tuple[FieldRef, ...] = (),
) -> TouchedAsset:
    return TouchedAsset(urn, "model.sql", (), removed, references)


def test_governance_reports_pii_exposure_from_replayed_column_lineage() -> None:
    evidence = GovernanceGate().collect(
        [_asset(removed=("cust_email",))], ReplayGovernanceGraph()
    )

    pii = next(item for item in evidence if item.kind == "pii_exposure")
    assert pii.subject == f"{CUSTOMERS}#cust_email"
    assert pii.detail["pii_tags"] == ["urn:li:tag:b2fd91.PII_Data"]
    assert pii.detail["unsafe_assets"]
    assert PolicyEngine().decide([pii]).decision == "BLOCK"


def test_governance_is_clean_for_a_field_without_pii_lineage() -> None:
    evidence = GovernanceGate().collect(
        [_asset(removed=("customer_id",))], ReplayGovernanceGraph()
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_governance_reports_a_deprecated_referenced_upstream() -> None:
    graph = ReplayGovernanceGraph(
        **{
            LEGACY: DatasetInfo(LEGACY, deprecated=True),
            CUSTOMERS: DatasetInfo(CUSTOMERS, owners=("urn:li:corpuser:owner",)),
        }
    )

    evidence = GovernanceGate().collect(
        [_asset(references=(FieldRef(LEGACY, "email"),))], graph
    )

    assert [(item.kind, item.subject) for item in evidence] == [
        ("deprecated_upstream", LEGACY)
    ]


def test_governance_is_clean_for_a_current_upstream() -> None:
    graph = ReplayGovernanceGraph(
        **{
            LEGACY: DatasetInfo(LEGACY, deprecated=False),
            CUSTOMERS: DatasetInfo(CUSTOMERS, owners=("urn:li:corpuser:owner",)),
        }
    )

    evidence = GovernanceGate().collect(
        [_asset(references=(FieldRef(LEGACY, "email"),))], graph
    )

    assert "deprecated_upstream" not in {item.kind for item in evidence}


def test_governance_reports_an_unowned_touched_asset() -> None:
    graph = ReplayGovernanceGraph(**{CUSTOMERS: DatasetInfo(CUSTOMERS)})

    evidence = GovernanceGate().collect([_asset()], graph)

    assert [(item.kind, item.subject) for item in evidence] == [
        ("unowned_asset", CUSTOMERS)
    ]


def test_governance_is_clean_for_an_owned_touched_asset() -> None:
    graph = ReplayGovernanceGraph(
        **{CUSTOMERS: DatasetInfo(CUSTOMERS, owners=("urn:li:corpuser:owner",))}
    )

    evidence = GovernanceGate().collect([_asset()], graph)

    assert "unowned_asset" not in {item.kind for item in evidence}


def test_governance_reports_restricted_data_routed_to_an_unrestricted_asset() -> None:
    graph = ReplayGovernanceGraph(
        **{
            RESTRICTED: DatasetInfo(
                RESTRICTED,
                fields=(SchemaField("email", "TEXT", True),),
                tags=("urn:li:tag:restricted",),
                owners=("urn:li:corpuser:owner",),
            ),
            PUBLIC: DatasetInfo(
                PUBLIC,
                fields=(SchemaField("email", "TEXT", True),),
                owners=("urn:li:corpuser:owner",),
            ),
        }
    )

    evidence = GovernanceGate().collect([_asset(RESTRICTED, removed=("email",))], graph)

    assert [(item.kind, item.subject) for item in evidence] == [
        ("access_policy_conflict", f"{RESTRICTED}#email")
    ]


def test_governance_skips_access_conflicts_without_explicit_restriction_metadata() -> None:
    graph = ReplayGovernanceGraph(
        **{
            RESTRICTED: DatasetInfo(
                RESTRICTED,
                fields=(SchemaField("email", "TEXT", True),),
                owners=("urn:li:corpuser:owner",),
            ),
            PUBLIC: DatasetInfo(
                PUBLIC,
                fields=(SchemaField("email", "TEXT", True),),
                owners=("urn:li:corpuser:owner",),
            ),
        }
    )

    evidence = GovernanceGate().collect([_asset(RESTRICTED, removed=("email",))], graph)

    assert "access_policy_conflict" not in {item.kind for item in evidence}


def test_default_policy_warns_for_the_new_governance_evidence() -> None:
    verdict = PolicyEngine().decide(
        [Evidence("access_policy_conflict", f"{RESTRICTED}#email", {})]
    )

    assert verdict.decision == "WARN"
    assert [finding.rule_id for finding in verdict.findings] == ["access_policy_conflict"]
