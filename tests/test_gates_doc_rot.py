from __future__ import annotations

from pathlib import Path

from sidq.gates.doc_rot import DocRotGate
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import Evidence, TouchedAsset
from sidq.policy.engine import PolicyEngine

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
FIXTURES = Path(__file__).parent / "fixtures" / "graph"


class ReplayDocumentationGraph(ReplayGraphClient):
    def __init__(self, documentation: dict[str, object] | None) -> None:
        super().__init__(FIXTURES)
        self._documentation = documentation

    def get_documentation(self, urn: str) -> dict[str, object] | None:
        assert urn == ORDERS
        return self._documentation


def _change(*, removed: tuple[str, ...] = (), added: tuple[str, ...] = ()) -> TouchedAsset:
    return TouchedAsset(ORDERS, "orders.sql", added, removed, ())


def test_doc_rot_reports_asset_and_field_descriptions_that_name_removed_columns() -> None:
    graph = ReplayDocumentationGraph(
        {
            "description": "The legacy `old_status` column remains available.",
            "field_descriptions": {
                "total": 'The renamed "old_status" value is retained for history.'
            },
        }
    )

    evidence = DocRotGate().collect([_change(removed=("old_status",))], graph)

    assert [(item.kind, item.subject) for item in evidence] == [
        ("doc_rot", ORDERS),
        ("doc_rot", f"{ORDERS}#total"),
    ]
    assert {item.detail["mentioned_field"] for item in evidence} == {"old_status"}


def test_doc_rot_is_clean_when_documentation_matches_the_post_change_schema() -> None:
    graph = ReplayDocumentationGraph(
        {"description": "The `total` column is the order amount."}
    )

    assert DocRotGate().collect([_change(removed=("old_status",))], graph) == []


def test_doc_rot_does_not_treat_ordinary_prose_as_a_column_reference() -> None:
    graph = ReplayDocumentationGraph(
        {"description": "The old column is retained only for historical context."}
    )

    assert DocRotGate().collect([_change(removed=("old_status",))], graph) == []


def test_doc_rot_skips_when_the_graph_does_not_supply_documentation() -> None:
    assert DocRotGate().collect([_change(removed=("old_status",))], ReplayDocumentationGraph(None)) == []


def test_default_policy_warns_for_change_scoped_doc_rot() -> None:
    verdict = PolicyEngine().decide([Evidence("doc_rot", ORDERS, {})])

    assert verdict.decision == "WARN"
    assert [finding.rule_id for finding in verdict.findings] == ["doc_rot"]
