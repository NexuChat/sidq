from __future__ import annotations

from pathlib import Path

from sidq.gates.governance import GovernanceGate
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import Evidence, FieldRef, TouchedAsset
from sidq.policy.engine import PolicyEngine

CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:dbt,b2fd91.order_entry_db.order_entry.customers,PROD)"
LEGACY = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.legacy_customers,PROD)"
RESTRICTED = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.restricted_customers,PROD)"
)
PUBLIC = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.public_customers,PROD)"
PII_SOURCE = "urn:li:dataset:(urn:li:dataPlatform:postgres,raw.customer_contact,PROD)"
PII_TARGET = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.customer_contact,PROD)"
)
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


class ProposedPIIRouteGraph:
    """A complete column route whose source field is explicitly classified."""

    def __init__(
        self,
        *,
        source_markers: tuple[str, ...] = ("urn:li:tag:PII_Data",),
        destination_markers: tuple[str, ...] = (),
        complete: bool = True,
    ) -> None:
        self.source_markers = source_markers
        self.destination_markers = destination_markers
        self.complete = complete

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        if urn == PII_SOURCE:
            return DatasetInfo(
                urn,
                fields=(
                    SchemaField(
                        "customer_email",
                        "TEXT",
                        True,
                        tags=self.source_markers,
                    ),
                    SchemaField(
                        "legacy_email",
                        "TEXT",
                        True,
                        tags=self.source_markers,
                    ),
                ),
                owners=("urn:li:corpuser:owner",),
            )
        if urn == PII_TARGET:
            return DatasetInfo(
                urn,
                fields=(SchemaField("customer_email", "TEXT", True),),
                tags=self.destination_markers,
                owners=("urn:li:corpuser:owner",),
            )
        return None

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        assert urn == PII_SOURCE
        assert column is not None
        return LineageResult(
            urns=(PII_TARGET,),
            columns={PII_TARGET: ("customer_email",)},
            tags={PII_TARGET: self.destination_markers},
            granularity="column",
            complete=self.complete,
        )


def _asset(
    urn: str = CUSTOMERS,
    *,
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    references: tuple[FieldRef, ...] = (),
) -> TouchedAsset:
    return TouchedAsset(urn, "model.sql", added, removed, references)


def test_removed_pii_field_does_not_create_pii_exposure() -> None:
    evidence = GovernanceGate().collect(
        [_asset(removed=("cust_email",))], ReplayGovernanceGraph()
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_existing_route_is_not_reclassified_as_exposure_without_a_route_delta() -> None:
    evidence = GovernanceGate().collect(
        [_asset(PII_SOURCE, added=("customer_email",))], ProposedPIIRouteGraph()
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_governance_never_queries_current_lineage_to_invent_route_causality() -> None:
    class MetadataOnlyGraph:
        def __init__(self) -> None:
            self.dataset_calls: list[str] = []
            self.lineage_calls = 0

        def get_dataset(self, urn: str) -> DatasetInfo | None:
            self.dataset_calls.append(urn)
            if urn == PII_SOURCE:
                return DatasetInfo(
                    urn,
                    fields=(
                        SchemaField(
                            "customer_email",
                            "TEXT",
                            True,
                            tags=("urn:li:tag:PII_Data",),
                        ),
                    ),
                    tags=("urn:li:tag:restricted",),
                    owners=("urn:li:corpuser:owner",),
                )
            if urn == LEGACY:
                return DatasetInfo(urn, deprecated=True)
            return None

        def get_downstream(
            self, urn: str, depth: int, column: str | None = None
        ) -> LineageResult:
            self.lineage_calls += 1
            raise AssertionError("GovernanceGate must not infer routes from lineage")

    graph = MetadataOnlyGraph()
    evidence = GovernanceGate().collect(
        [
            _asset(
                PII_SOURCE,
                added=("customer_email",),
                removed=("legacy_email",),
                references=(FieldRef(LEGACY, "email"),),
            )
        ],
        graph,
    )

    assert graph.dataset_calls == [PII_SOURCE, LEGACY]
    assert graph.lineage_calls == 0
    assert [(item.kind, item.subject) for item in evidence] == [
        ("deprecated_upstream", LEGACY)
    ]


def test_downstream_asset_pii_tag_does_not_classify_an_added_source_field() -> None:
    evidence = GovernanceGate().collect(
        [_asset(added=("cust_email",))], ReplayGovernanceGraph()
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_added_unclassified_field_does_not_create_pii_exposure() -> None:
    evidence = GovernanceGate().collect(
        [_asset(PII_SOURCE, added=("customer_email",))],
        ProposedPIIRouteGraph(source_markers=()),
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_added_pii_route_to_restricted_destination_is_not_false_exposure() -> None:
    evidence = GovernanceGate().collect(
        [_asset(PII_SOURCE, added=("customer_email",))],
        ProposedPIIRouteGraph(destination_markers=("urn:li:tag:restricted",)),
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_incomplete_lineage_never_becomes_high_confidence_pii_exposure() -> None:
    evidence = GovernanceGate().collect(
        [_asset(PII_SOURCE, added=("customer_email",))],
        ProposedPIIRouteGraph(complete=False),
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


def test_rename_checks_only_the_new_field_for_governance_exposure() -> None:
    evidence = GovernanceGate().collect(
        [
            _asset(
                PII_SOURCE,
                added=("customer_email",),
                removed=("legacy_email",),
            )
        ],
        ProposedPIIRouteGraph(),
    )

    assert "pii_exposure" not in {item.kind for item in evidence}


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


def test_governance_does_not_infer_access_conflict_without_a_route_delta() -> None:
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

    evidence = GovernanceGate().collect([_asset(RESTRICTED, added=("email",))], graph)

    assert "access_policy_conflict" not in {item.kind for item in evidence}


def test_removed_restricted_field_does_not_create_access_policy_conflict() -> None:
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

    assert "access_policy_conflict" not in {item.kind for item in evidence}


def test_governance_skips_access_conflicts_without_explicit_restriction_metadata() -> (
    None
):
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

    evidence = GovernanceGate().collect([_asset(RESTRICTED, added=("email",))], graph)

    assert "access_policy_conflict" not in {item.kind for item in evidence}


def test_default_policy_warns_for_the_new_governance_evidence() -> None:
    verdict = PolicyEngine().decide(
        [Evidence("access_policy_conflict", f"{RESTRICTED}#email", {})]
    )

    assert verdict.decision == "WARN"
    assert [finding.rule_id for finding in verdict.findings] == [
        "access_policy_conflict"
    ]
