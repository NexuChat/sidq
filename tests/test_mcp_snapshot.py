"""The MCP read path: does the agent run on the official surface, and admit its bounds?

`from_datahub` reads through the DataHub Python SDK. That is a legitimate client,
but a submission claiming its agent works over the official MCP server should have
an agent that actually does. These pin the MCP path's two hard parts: it reads only
through tools `mcp-server-datahub` exposes, and — because column lineage costs one
call per column — it never lets an asset it could not afford to read pass for a
clean one.
"""

from __future__ import annotations

from typing import Any

import pytest

from sidq.agent import CatalogAuditor, receipts_for
from sidq.gates.self_contradiction import CatalogSnapshot, SelfContradictionGate
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField

_PLATFORM = "urn:li:dataPlatform:dbt"


def _urn(name: str) -> str:
    return f"urn:li:dataset:(urn:{_PLATFORM},warehouse.{name},PROD)"


class _FakeMCPGraph:
    """Only the four read tools `mcp-server-datahub` exposes, and nothing else."""

    def __init__(
        self,
        datasets: dict[str, DatasetInfo],
        table_lineage: dict[str, tuple[str, ...]],
        column_lineage: dict[tuple[str, str], dict[str, tuple[str, ...]]] | None = None,
        fail_columns_for: frozenset[str] = frozenset(),
    ) -> None:
        self._datasets = datasets
        self._table = table_lineage
        self._columns = column_lineage or {}
        self._fail = fail_columns_for
        self.column_calls: list[tuple[str, str]] = []

    def _search(self, query: str) -> Any:
        del query
        return {"searchResults": [{"entity": {"urn": urn}} for urn in self._datasets]}

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self._datasets.get(urn)

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        del depth
        if column is None:
            targets = self._table.get(urn, ())
            return LineageResult(urns=targets, columns=dict.fromkeys(targets, ()))
        if urn in self._fail:
            raise RuntimeError("lineage tool refused")
        self.column_calls.append((urn, column))
        return LineageResult(
            urns=tuple(self._columns.get((urn, column), {})),
            columns=self._columns.get((urn, column), {}),
            granularity="column",
        )


def _dataset(name: str, *fields: str, owners: tuple[str, ...] = ()) -> DatasetInfo:
    return DatasetInfo(
        urn=_urn(name),
        fields=tuple(
            SchemaField(path=field, native_type="STRING", nullable=True)
            for field in fields
        ),
        owners=owners,
    )


def _graph(**kwargs: Any) -> _FakeMCPGraph:
    datasets = {
        _urn("orders"): _dataset("orders", "id", "email"),
        _urn("mart"): _dataset("mart", "id"),
    }
    table = {_urn("orders"): (_urn("mart"),)}
    columns = {
        (_urn("orders"), "id"): {_urn("mart"): ("id",)},
        (_urn("orders"), "email"): {_urn("mart"): ("customer_email",)},
    }
    return _FakeMCPGraph(datasets, table, columns, **kwargs)


def test_from_mcp_uses_only_the_official_read_tools() -> None:
    """The fake exposes nothing else; anything extra would raise AttributeError."""
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=10)

    assert {entity.urn for entity in snapshot.entities} == {
        _urn("orders"),
        _urn("mart"),
    }
    assert snapshot.field_lineage_resolved == frozenset({_urn("orders"), _urn("mart")})


def test_from_mcp_rejects_a_client_without_the_mcp_read_surface() -> None:
    with pytest.raises(Exception, match="MCP read surface"):
        CatalogSnapshot.from_mcp(object())


def test_column_lineage_is_only_fetched_for_the_budgeted_assets() -> None:
    """Field lineage costs one call per column, so a budget of zero spends none."""
    graph = _graph()

    snapshot = CatalogSnapshot.from_mcp(graph, field_lineage_budget=0)

    assert graph.column_calls == []
    assert snapshot.field_lineage_resolved == frozenset()
    assert all(edge.target_field is None for edge in snapshot.edges)


def test_the_budget_goes_to_the_most_connected_asset_first() -> None:
    graph = _graph()

    CatalogSnapshot.from_mcp(graph, field_lineage_budget=1)

    assert {urn for urn, _ in graph.column_calls} == {_urn("orders")}


def test_field_edges_carry_the_column_names_the_lineage_tool_returned() -> None:
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=10)

    assert (_urn("orders"), "email", _urn("mart"), "customer_email") in {
        (edge.source_urn, edge.source_field, edge.target_urn, edge.target_field)
        for edge in snapshot.edges
    }


def test_an_asset_whose_columns_failed_is_not_recorded_as_resolved() -> None:
    """Partial success is the dangerous case: it would look identical to clean."""
    graph = _graph(fail_columns_for=frozenset({_urn("orders")}))

    snapshot = CatalogSnapshot.from_mcp(graph, field_lineage_budget=10)

    assert _urn("orders") not in (snapshot.field_lineage_resolved or frozenset())
    assert any(edge.target_urn == _urn("mart") for edge in snapshot.edges)


def test_an_unresolved_asset_is_reported_unverifiable_not_silent() -> None:
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=0)

    kinds = {
        (item.kind, item.subject)
        for item in SelfContradictionGate().collect((), _Supplied(snapshot))
    }

    assert ("lineage_field_missing_unverifiable", _urn("orders")) in kinds


def test_the_sdk_path_is_not_burdened_with_a_field_lineage_bound() -> None:
    """`None` means every asset's field lineage is present, so nothing is unresolved."""
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=10)
    complete = CatalogSnapshot(snapshot.entities, snapshot.edges)

    assert complete.field_lineage_resolved is None
    assert not [
        item
        for item in SelfContradictionGate().collect((), _Supplied(complete))
        if item.kind == "lineage_field_missing_unverifiable"
    ]


def test_an_asset_that_could_not_be_checked_is_never_called_clean() -> None:
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=0)

    result = CatalogAuditor(snapshot).run()

    assert _urn("mart") in result.unestablished
    assert _urn("mart") not in result.verified
    assert result.summary()["unestablished"] >= 1


def test_no_receipt_is_written_for_an_asset_nothing_was_established_about() -> None:
    """The policy calls unverifiable evidence informational, so the engine says PASS.

    Writing that would stamp `sidq:verified` on an asset nothing was proved about.
    """
    snapshot = CatalogSnapshot.from_mcp(_graph(), field_lineage_budget=0)
    result = CatalogAuditor(snapshot).run()

    written = {receipt.urn for receipt in receipts_for(result)}

    assert result.unestablished
    assert not written & set(result.unestablished)


class _Supplied:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self._snapshot
