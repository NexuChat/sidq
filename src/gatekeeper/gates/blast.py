"""Gate 2: record the downstream impact and the actual lineage paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sidq.gates.base import graph_unavailable
from sidq.graph.client import DatasetInfo, GraphClient, LineagePath, LineageResult
from sidq.models import Evidence, TouchedAsset


class BlastRadiusGate:
    id = "blast"

    def __init__(self, depth: int = 3) -> None:
        self._depth = depth

    def collect(self, change: Sequence[TouchedAsset], graph: GraphClient) -> list[Evidence]:
        evidence: list[Evidence] = []
        for asset in sorted(change, key=lambda item: item.urn):
            column = _changed_column(asset)
            try:
                result = graph.get_downstream(asset.urn, self._depth, column=column)
                # A response which cannot honestly claim column granularity gets a table retry.
                if column is not None and result.granularity != "column":
                    result = graph.get_downstream(asset.urn, self._depth, column=None)
                paths = _paths(graph, asset.urn, result)
                details = _details(graph, asset.urn, result, paths, self._depth)
            except Exception as error:  # noqa: BLE001 - graph transports may raise arbitrary client errors
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            evidence.append(Evidence("blast_radius", asset.urn, details))
        return evidence


def _changed_column(asset: TouchedAsset) -> str | None:
    fields = tuple(sorted(set(asset.removed_fields or asset.added_fields)))
    return fields[0] if len(fields) == 1 else None


def _paths(graph: GraphClient, source: str, result: LineageResult) -> list[LineagePath]:
    paths = list(result.paths)
    for downstream in result.urns:
        paths.extend(graph.paths_between(source, downstream))
    unique_paths = sorted({(path.urns, path.granularity) for path in paths})
    return [LineagePath(urns, granularity) for urns, granularity in unique_paths]


def _details(graph: GraphClient, source: str, result: LineageResult, paths: Sequence[LineagePath], depth: int) -> dict[str, object]:
    source_info = graph.get_dataset(source)
    source_owners = set(source_info.owners if source_info else ())
    dashboards: list[str] = []
    critical: list[str] = []
    cross_team: list[str] = []
    for urn in result.urns:
        entity_type = result.entity_types.get(urn, "") if isinstance(result.entity_types, Mapping) else ""
        info: DatasetInfo | None = graph.get_dataset(urn)
        if entity_type.lower() == "dashboard" or "dashboard" in urn.lower():
            dashboards.append(urn)
        if info is not None:
            if any("critical" in tag.lower() for tag in info.tags):
                critical.append(urn)
            cross_team.extend(owner for owner in info.owners if source_owners and owner not in source_owners)
    return {
        "downstream_count": len(result.urns),
        "downstream_urns": sorted(result.urns),
        "paths": [{"urns": list(path.urns), "granularity": path.granularity} for path in paths],
        "dashboards": sorted(set(dashboards)),
        "critical_assets": sorted(set(critical)),
        "cross_team_owners": sorted(set(cross_team)),
        "depth": depth,
        "granularity": result.granularity if result.granularity in {"column", "table"} else "table",
    }
