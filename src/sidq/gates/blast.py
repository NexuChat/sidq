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

    def collect(
        self, change: Sequence[TouchedAsset], graph: GraphClient
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        for asset in sorted(change, key=lambda item: item.urn):
            column = _changed_column(asset)
            try:
                result = graph.get_downstream(asset.urn, self._depth, column=column)
                # A response which cannot honestly claim column granularity gets a table retry.
                if column is not None and result.granularity != "column":
                    result = graph.get_downstream(asset.urn, self._depth, column=None)
                result, bi_paths = _with_bi_consumers(graph, result)
                paths = _paths(graph, asset.urn, result, column)
                details = _details(
                    graph, asset.urn, result, [*paths, *bi_paths], self._depth
                )
            except Exception as error:  # noqa: BLE001 - graph transports may raise arbitrary client errors
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            evidence.append(Evidence("blast_radius", asset.urn, details))
            pii_tags = details["pii_tags"]
            if column is not None and pii_tags and details["dashboards"]:
                evidence.append(
                    Evidence(
                        "pii_exposure",
                        f"{asset.urn}#{column}",
                        {
                            "changed_field": column,
                            "pii_tags": pii_tags,
                            "tagged_assets": details["pii_assets"],
                            "dashboards": details["dashboards"],
                            "paths": details["paths"],
                        },
                    )
                )
        return evidence


def _changed_column(asset: TouchedAsset) -> str | None:
    fields = tuple(sorted(set(asset.removed_fields or asset.added_fields)))
    return fields[0] if len(fields) == 1 else None


def _with_bi_consumers(
    graph: GraphClient, result: LineageResult
) -> tuple[LineageResult, list[LineagePath]]:
    """Charts and dashboards are entity-level hops after field-level lineage ends."""
    extra_urns: list[str] = []
    extra_types: dict[str, str] = {}
    extra_tags: dict[str, tuple[str, ...]] = {}
    extra_paths: list[LineagePath] = []
    for urn in result.urns:
        if "looker" not in urn or ".explore." not in urn:
            continue
        consumers = graph.get_downstream(urn, 2)
        extra_urns.extend(consumers.urns)
        if isinstance(consumers.entity_types, Mapping):
            extra_types.update(consumers.entity_types)
        if isinstance(consumers.tags, Mapping):
            extra_tags.update(consumers.tags)
        dashboard = next(
            (
                consumer
                for consumer in consumers.urns
                if consumers.entity_types.get(consumer, "").lower() == "dashboard"
                or consumer.startswith("urn:li:dashboard:")
            ),
            None,
        )
        chart = next(
            (
                consumer
                for consumer in consumers.urns
                if consumers.entity_types.get(consumer, "").lower() == "chart"
                or consumer.startswith("urn:li:chart:")
            ),
            None,
        )
        if chart is not None:
            extra_paths.extend(graph.paths_between(urn, chart))
        if chart is not None and dashboard is not None:
            extra_paths.extend(graph.paths_between(chart, dashboard))
    return (
        LineageResult(
            urns=tuple(sorted({*result.urns, *extra_urns})),
            entity_types={**dict(result.entity_types), **extra_types},
            paths=result.paths,
            columns=result.columns,
            tags={**dict(result.tags), **extra_tags},
            granularity=result.granularity,
        ),
        extra_paths,
    )


def _paths(
    graph: GraphClient, source: str, result: LineageResult, source_column: str | None
) -> list[LineagePath]:
    paths = list(result.paths)
    downstream = next(
        (urn for urn in result.urns if "looker" in urn and ".explore." in urn),
        next(iter(result.urns), None),
    )
    if downstream is not None:
        target_columns = (
            result.columns.get(downstream, ())
            if isinstance(result.columns, Mapping)
            else ()
        )
        if source_column is not None and target_columns:
            paths.extend(
                graph.paths_between(
                    source, downstream, source_column, target_columns[0]
                )
            )
        else:
            paths.extend(graph.paths_between(source, downstream))
    unique_paths = sorted({(path.urns, path.granularity) for path in paths})
    return [LineagePath(urns, granularity) for urns, granularity in unique_paths]


def _details(
    graph: GraphClient,
    source: str,
    result: LineageResult,
    paths: Sequence[LineagePath],
    depth: int,
) -> dict[str, object]:
    source_info = graph.get_dataset(source)
    source_owners = set(source_info.owners if source_info else ())
    dashboards: list[str] = []
    critical: list[str] = []
    cross_team: list[str] = []
    pii_assets: dict[str, list[str]] = {}
    for urn in result.urns:
        entity_type = (
            result.entity_types.get(urn, "")
            if isinstance(result.entity_types, Mapping)
            else ""
        )
        inline_tags = result.tags.get(urn) if isinstance(result.tags, Mapping) else None
        # Lineage responses include tags but not ownership.  Read entity metadata
        # even when tags are inline so criticality and cross-team ownership cannot
        # silently disappear from an otherwise complete blast-radius verdict.
        try:
            info: DatasetInfo | None = graph.get_dataset(urn)
        except Exception:  # noqa: BLE001 -- optional enrichment must not discard proven lineage
            info = None
        tags = info.tags if info is not None else (inline_tags or ())
        if entity_type.lower() == "dashboard" or urn.startswith("urn:li:dashboard:"):
            dashboards.append(urn)
        if info is not None:
            # This graph seam has no separate criticality field: only an explicit
            # tag containing "critical" is evidence.  An untagged asset must stay
            # out of critical_assets rather than being inferred from its name.
            if any("critical" in tag.lower() for tag in tags):
                critical.append(urn)
            cross_team.extend(
                owner
                for owner in info.owners
                if source_owners and owner not in source_owners
            )
            pii = sorted(tag for tag in tags if "pii" in tag.lower())
            if pii:
                pii_assets[urn] = pii
        elif tags:
            pii = sorted(tag for tag in tags if "pii" in tag.lower())
            if pii:
                pii_assets[urn] = pii
    return {
        "downstream_count": len(result.urns),
        "downstream_urns": sorted(result.urns),
        "paths": [_path_detail(path) for path in paths],
        "dashboards": sorted(set(dashboards)),
        "critical_assets": sorted(set(critical)),
        "cross_team_owners": sorted(set(cross_team)),
        "pii_tags": sorted({tag for tags in pii_assets.values() for tag in tags}),
        "pii_assets": {urn: pii_assets[urn] for urn in sorted(pii_assets)},
        "depth": depth,
        "granularity": result.granularity
        if result.granularity in {"column", "table"}
        else "table",
    }


def _path_detail(path: LineagePath) -> dict[str, object]:
    """Keep the direction of a lineage path through canonical JSON serialization.

    The artifact serializer sorts arrays for determinism. Numbered edge keys retain
    the source-to-target order while remaining ordinary JSON data.
    """
    return {
        "granularity": path.granularity,
        "source": path.urns[0] if path.urns else "",
        "target": path.urns[-1] if path.urns else "",
        "hops": {
            f"{index:03d}": {"from": left, "to": right}
            for index, (left, right) in enumerate(zip(path.urns, path.urns[1:]))
        },
    }
