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
                result = _with_bi_consumers(graph, result)
                paths = _paths(graph, asset.urn, result, column)
                details = _details(graph, asset.urn, result, paths, self._depth)
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
) -> LineageResult:
    """Charts and dashboards are entity-level hops after field-level lineage ends."""
    extra_urns: list[str] = []
    extra_types: dict[str, str] = {}
    extra_tags: dict[str, tuple[str, ...]] = {}
    for urn in result.urns:
        if "looker" not in urn or ".explore." not in urn:
            continue
        consumers = graph.get_downstream(urn, 2)
        extra_urns.extend(consumers.urns)
        consumer_types = (
            consumers.entity_types
            if isinstance(consumers.entity_types, Mapping)
            else {}
        )
        consumer_tags = consumers.tags if isinstance(consumers.tags, Mapping) else {}
        extra_types.update(consumer_types)
        extra_tags.update(consumer_tags)
    return LineageResult(
        urns=tuple(sorted({*result.urns, *extra_urns})),
        entity_types={
            **(
                dict(result.entity_types)
                if isinstance(result.entity_types, Mapping)
                else {}
            ),
            **extra_types,
        },
        paths=result.paths,
        columns=result.columns,
        tags={
            **(dict(result.tags) if isinstance(result.tags, Mapping) else {}),
            **extra_tags,
        },
        granularity=result.granularity,
    )


def _paths(
    graph: GraphClient,
    source: str,
    result: LineageResult,
    source_column: str | None,
) -> list[LineagePath]:
    downstream = next(
        (urn for urn in result.urns if "looker" in urn and ".explore." in urn),
        next(iter(result.urns), None),
    )
    dashboard = next(
        (urn for urn in result.urns if urn.startswith("urn:li:dashboard:")), None
    )
    if downstream is None or dashboard is None:
        return []
    if source_column is not None:
        target_columns = (
            result.columns.get(downstream, ())
            if isinstance(result.columns, Mapping)
            else ()
        )
        if target_columns:
            column_paths = graph.paths_between(
                source, downstream, source_column, target_columns[0]
            )
            entity_paths = graph.paths_between(downstream, dashboard)
            combined = _combine_column_and_bi_paths(
                column_paths, entity_paths, downstream, result.granularity
            )
            if combined:
                return combined
    return [
        LineagePath(path.urns, result.granularity, path.note)
        for path in graph.paths_between(source, dashboard)
    ]


def _combine_column_and_bi_paths(
    column_paths: Sequence[LineagePath],
    entity_paths: Sequence[LineagePath],
    handoff_dataset: str,
    granularity: str,
) -> list[LineagePath]:
    """Join two independently oriented paths at the field-to-entity boundary."""
    if granularity != "column":
        return []
    for column_path in column_paths:
        if not column_path.urns or column_path.granularity != "column":
            continue
        for entity_path in entity_paths:
            if not entity_path.urns or entity_path.urns[0] != handoff_dataset:
                continue
            urns = (*column_path.urns, handoff_dataset, *entity_path.urns[1:])
            return [
                LineagePath(
                    urns,
                    "column",
                    "Column lineage is proven through the BI field; chart and dashboard hops are entity-level.",
                )
            ]
    return []


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
    detail: dict[str, object] = {
        "granularity": path.granularity,
        "source": path.urns[0] if path.urns else "",
        "target": path.urns[-1] if path.urns else "",
        "hops": {
            f"{index:03d}": {"from": left, "to": right}
            for index, (left, right) in enumerate(zip(path.urns, path.urns[1:]))
        },
    }
    if path.note:
        detail["note"] = path.note
    return detail
