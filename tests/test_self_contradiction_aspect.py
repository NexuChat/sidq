"""The opt-in stored-aspect field-lineage boundary and its fail-closed rules."""

from __future__ import annotations

from typing import Any

from sidq.gates.self_contradiction import CatalogSnapshot, SelfContradictionGate
from sidq.graph.client import DatasetInfo, LineageResult, SchemaField

_PLATFORM = "urn:li:dataPlatform:dbt"


def _urn(name: str) -> str:
    return f"urn:li:dataset:({_PLATFORM},warehouse.{name},PROD)"


def _field_urn(dataset: str, field: str) -> str:
    return f"urn:li:schemaField:({dataset},{field})"


def _dataset(name: str, *fields: str) -> DatasetInfo:
    return DatasetInfo(
        urn=_urn(name),
        fields=tuple(
            SchemaField(path=field, native_type="STRING", nullable=True)
            for field in fields
        ),
    )


class _Graph:
    def __init__(self) -> None:
        self.datasets = {
            _urn("orders"): _dataset("orders", "id", "email"),
            _urn("mart"): _dataset("mart", "id", "customer_email"),
        }
        self.column_calls: list[tuple[str, str]] = []

    def _search(self, query: str) -> Any:
        del query
        return {"searchResults": [{"entity": {"urn": urn}} for urn in self.datasets]}

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self.datasets.get(urn)

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        del depth
        if column is not None:
            self.column_calls.append((urn, column))
            return LineageResult(granularity="column")
        targets = (_urn("mart"),) if urn == _urn("orders") else ()
        return LineageResult(urns=targets, columns=dict.fromkeys(targets, ()))


class _AspectReader:
    def __init__(
        self,
        payloads: dict[str, dict[str, Any] | None],
        *,
        fail_for: frozenset[str] = frozenset(),
    ) -> None:
        self.payloads = payloads
        self.fail_for = fail_for
        self.calls: list[tuple[str, str]] = []

    def get_aspect_json(self, urn: str, aspect: str) -> dict[str, Any] | None:
        self.calls.append((urn, aspect))
        if urn in self.fail_for:
            raise OSError("catalog read failed")
        return self.payloads.get(urn)


def _aspect(*records: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 0,
        "aspect": {
            "com.linkedin.dataset.UpstreamLineage": {
                "upstreams": [{"dataset": _urn("orders")}],
                "fineGrainedLineages": list(records),
            }
        },
    }


def _empty_aspect() -> dict[str, Any]:
    return _aspect()


def _fine_edge(*, source: str, target: str) -> dict[str, Any]:
    return {
        "upstreamType": "FIELD_SET",
        "downstreamType": "FIELD",
        "upstreams": [_field_urn(_urn("orders"), source)],
        "downstreams": [_field_urn(_urn("mart"), target)],
    }


def test_opt_in_aspect_path_reads_each_budgeted_dataset_once_and_all_edges() -> None:
    graph = _Graph()
    reader = _AspectReader(
        {
            _urn("orders"): _empty_aspect(),
            _urn("mart"): _aspect(
                _fine_edge(source="id", target="id"),
                _fine_edge(source="email", target="customer_email"),
            ),
        }
    )

    snapshot = CatalogSnapshot.from_mcp(
        graph, field_lineage_budget=10, field_lineage_reader=reader
    )

    assert graph.column_calls == []
    assert reader.calls == [
        (_urn("mart"), "upstreamLineage"),
        (_urn("orders"), "upstreamLineage"),
    ]
    assert snapshot.field_lineage_resolved == frozenset({_urn("orders"), _urn("mart")})
    assert {
        (edge.source_urn, edge.source_field, edge.target_urn, edge.target_field)
        for edge in snapshot.edges
        if edge.source_field is not None
    } == {
        (_urn("orders"), "id", _urn("mart"), "id"),
        (_urn("orders"), "email", _urn("mart"), "customer_email"),
    }


def test_one_aspect_read_decodes_the_measured_58_edge_wide_asset_shape() -> None:
    records = [
        _fine_edge(source=f"source_{index}", target=f"TARGET_{index}")
        for index in range(57)
    ]
    # The live asset has 58 records over 57 distinct upstream columns: one
    # upstream column participates in a second fine-grained edge.
    records.append(_fine_edge(source="source_0", target="SECOND_TARGET"))
    reader = _AspectReader(
        {
            _urn("orders"): _empty_aspect(),
            _urn("mart"): _aspect(*records),
        }
    )

    snapshot = CatalogSnapshot.from_mcp(
        _Graph(), field_lineage_budget=10, field_lineage_reader=reader
    )

    assert reader.calls.count((_urn("mart"), "upstreamLineage")) == 1
    field_edges = [
        edge
        for edge in snapshot.edges
        if edge.target_urn == _urn("mart") and edge.source_field is not None
    ]
    assert len(field_edges) == 58
    assert len({edge.source_field for edge in field_edges}) == 57


def test_aspect_reader_is_never_discovered_or_used_without_explicit_opt_in() -> None:
    graph = _Graph()
    graph.get_aspect_json = lambda urn, aspect: (_ for _ in ()).throw(
        AssertionError("direct aspect read must remain opt-in")
    )

    CatalogSnapshot.from_mcp(graph, field_lineage_budget=10)

    assert graph.column_calls == [
        (_urn("mart"), "id"),
        (_urn("mart"), "customer_email"),
        (_urn("orders"), "id"),
        (_urn("orders"), "email"),
    ]


def test_missing_or_unreadable_aspect_is_unresolved_not_empty_lineage() -> None:
    for reader in (
        _AspectReader({_urn("orders"): _empty_aspect(), _urn("mart"): None}),
        _AspectReader(
            {_urn("orders"): _empty_aspect()}, fail_for=frozenset({_urn("mart")})
        ),
    ):
        snapshot = CatalogSnapshot.from_mcp(
            _Graph(), field_lineage_budget=10, field_lineage_reader=reader
        )

        assert _urn("mart") not in (snapshot.field_lineage_resolved or frozenset())
        assert any(
            edge.source_urn == _urn("orders")
            and edge.target_urn == _urn("mart")
            and edge.source_field is None
            for edge in snapshot.edges
        )
        evidence = SelfContradictionGate().collect((), _Supplied(snapshot))
        assert any(
            item.kind == "lineage_field_missing_unverifiable"
            and item.subject == _urn("mart")
            for item in evidence
        )


def test_malformed_aspect_is_unresolved_instead_of_partially_parsed() -> None:
    malformed = _aspect(_fine_edge(source="id", target="id"))
    lineage = malformed["aspect"]["com.linkedin.dataset.UpstreamLineage"]
    lineage["fineGrainedLineages"].append(
        {
            "upstreams": ["not-a-schema-field-urn"],
            "downstreams": [_field_urn(_urn("mart"), "customer_email")],
        }
    )
    snapshot = CatalogSnapshot.from_mcp(
        _Graph(),
        field_lineage_budget=10,
        field_lineage_reader=_AspectReader(
            {_urn("orders"): _empty_aspect(), _urn("mart"): malformed}
        ),
    )

    assert _urn("mart") not in (snapshot.field_lineage_resolved or frozenset())
    assert not [edge for edge in snapshot.edges if edge.source_field is not None]


class _Supplied:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    def catalog_snapshot(self) -> CatalogSnapshot:
        return self.snapshot


def test_a_binding_budget_is_spent_on_the_asset_that_holds_the_aspect() -> None:
    """The two paths read opposite directions, so they cannot share one ranking.

    `get_lineage` resolves from the source outward, so most-consumed-first
    points it at assets it can resolve. `upstreamLineage` is stored on the
    target, so the same ordering points at pure sources — which hold no such
    aspect — and spends the whole budget on the one set guaranteed to return
    nothing, while the marts holding every fine-grained edge rank last and fall
    outside it first.

    Every other test in this file uses a budget of ten against a two-asset
    graph, which makes the budget non-binding and hides this completely. This
    one sets it to one, where the choice of ordering is the entire result.
    """
    reader = _AspectReader(
        {
            _urn("mart"): _aspect(_fine_edge(source="email", target="customer_email")),
            _urn("orders"): None,  # a pure source has no upstreamLineage at all
        }
    )

    snapshot = CatalogSnapshot.from_mcp(
        _Graph(), field_lineage_budget=1, field_lineage_reader=reader
    )

    read = [urn for urn, _aspect_name in reader.calls]
    assert read == [_urn("mart")], (
        "the single affordable read was spent on an asset with no aspect"
    )

    assert snapshot.field_lineage_resolved == frozenset({_urn("mart")})
    assert {
        (edge.source_field, edge.target_field)
        for edge in snapshot.edges
        if edge.source_field is not None
    } == {("email", "customer_email")}
