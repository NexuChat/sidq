"""Gate 0: make catalog drift visible at review time."""

from __future__ import annotations

from collections.abc import Sequence

from sidq.gates.base import graph_unavailable
from sidq.graph.client import GraphClient, SchemaField
from sidq.graph.live_source import LiveSourceClient
from sidq.models import Evidence, TouchedAsset


class RealityGate:
    id = "reality"

    def __init__(self, source: LiveSourceClient) -> None:
        self._source = source

    def collect(self, change: Sequence[TouchedAsset], graph: GraphClient) -> list[Evidence]:
        evidence: list[Evidence] = []
        for asset in sorted(change, key=lambda item: item.urn):
            try:
                catalog = graph.get_dataset(asset.urn)
                source = self._source.get_dataset(asset.urn)
            except Exception as error:  # noqa: BLE001 - graph transports may raise arbitrary client errors
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            if catalog is None or source is None:
                # An absent side cannot prove reality; make the failed prerequisite visible.
                evidence.append(Evidence("graph_unavailable", asset.urn, {"error": "dataset_not_found"}))
                continue
            graph_fields = _field_data(catalog.fields)
            live_fields = _field_data(source.fields)
            graph_paths = {field.path for field in catalog.fields}
            live_paths = {field.path for field in source.fields}
            if graph_fields != live_fields:
                evidence.append(
                    Evidence(
                        "catalog_reality_mismatch",
                        asset.urn,
                        {
                            "graph_fields": graph_fields,
                            "live_fields": live_fields,
                            "missing_in_graph": sorted(live_paths - graph_paths),
                            "missing_in_source": sorted(graph_paths - live_paths),
                        },
                    )
                )
        return evidence


def _field_data(fields: Sequence[SchemaField]) -> list[dict[str, object]]:
    return [
        {"path": field.path, "native_type": field.native_type, "nullable": field.nullable}
        for field in sorted(fields, key=lambda field: field.path)
    ]
