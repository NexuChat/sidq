"""Gate 3: collect explicit governance facts about a proposed change."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sidq.gates.base import graph_unavailable
from sidq.graph.client import DatasetInfo, GraphClient, LineageResult
from sidq.models import Evidence, TouchedAsset

_RESTRICTION_WORDS = ("restricted", "confidential", "sensitive", "private")


class GovernanceGate:
    """Report governance evidence without assigning a severity or verdict."""

    id = "governance"

    def __init__(self, depth: int = 3) -> None:
        self._depth = depth

    def collect(
        self, change: Sequence[TouchedAsset], graph: GraphClient
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        datasets: dict[str, DatasetInfo | None] = {}

        def dataset(urn: str) -> DatasetInfo | None:
            if urn not in datasets:
                datasets[urn] = graph.get_dataset(urn)
            return datasets[urn]

        for asset in sorted(change, key=lambda item: item.urn):
            try:
                asset_info = dataset(asset.urn)
            except Exception as error:  # noqa: BLE001 - graph transports vary
                evidence.append(graph_unavailable(asset.urn, error))
                continue

            if asset_info is not None and not asset_info.owners:
                evidence.append(
                    Evidence(
                        "unowned_asset",
                        asset.urn,
                        {"owners": [], "confidence": "high"},
                    )
                )

            evidence.extend(self._deprecated_upstreams(asset, dataset, evidence))
            for field in _changed_fields(asset):
                try:
                    downstream = graph.get_downstream(
                        asset.urn, self._depth, column=field
                    )
                except Exception as error:  # noqa: BLE001 - graph transports vary
                    evidence.append(graph_unavailable(f"{asset.urn}#{field}", error))
                    continue
                evidence.extend(_pii_exposure(asset, field, asset_info, downstream))
                evidence.extend(
                    _access_policy_conflicts(
                        asset, field, asset_info, downstream, dataset
                    )
                )
        return sorted(
            evidence, key=lambda item: (item.kind, item.subject, repr(item.detail))
        )

    def _deprecated_upstreams(
        self,
        asset: TouchedAsset,
        dataset: object,
        evidence: list[Evidence],
    ) -> list[Evidence]:
        results: list[Evidence] = []
        upstreams = sorted(
            {reference.dataset_urn for reference in asset.referenced_fields}
        )
        for upstream in upstreams:
            try:
                info = dataset(upstream)  # type: ignore[operator]
            except Exception as error:  # noqa: BLE001 - graph transports vary
                evidence.append(graph_unavailable(upstream, error))
                continue
            if info is not None and info.deprecated:
                results.append(
                    Evidence(
                        "deprecated_upstream",
                        upstream,
                        {
                            "referenced_by": asset.urn,
                            "referenced_fields": sorted(
                                {
                                    reference.field_path
                                    for reference in asset.referenced_fields
                                    if reference.dataset_urn == upstream
                                }
                            ),
                            "confidence": "high",
                        },
                    )
                )
        return results


def _changed_fields(asset: TouchedAsset) -> tuple[str, ...]:
    return tuple(sorted({*asset.added_fields, *asset.removed_fields}))


def _flowing_targets(result: LineageResult) -> tuple[str, ...]:
    """Only regard targets with returned column mappings as a proven field route."""
    if not isinstance(result.columns, Mapping):
        return ()
    return tuple(
        sorted(urn for urn in result.urns if tuple(result.columns.get(urn, ())))
    )


def _tags(result: LineageResult, urn: str) -> tuple[str, ...]:
    if not isinstance(result.tags, Mapping):
        return ()
    return tuple(sorted(set(result.tags.get(urn, ()))))


def _pii(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(value for value in values if "pii" in value.lower()))


def _pii_exposure(
    asset: TouchedAsset,
    field: str,
    source: DatasetInfo | None,
    downstream: LineageResult,
) -> list[Evidence]:
    targets = _flowing_targets(downstream)
    if not targets:
        return []

    tagged_assets: dict[str, list[str]] = {}
    if source is not None:
        source_tags = _pii((*source.tags, *source.terms))
        if source_tags:
            tagged_assets[asset.urn] = list(source_tags)
    for target in targets:
        target_tags = _pii(_tags(downstream, target))
        if target_tags:
            tagged_assets[target] = list(target_tags)

    if not tagged_assets:
        return []
    unsafe_assets = sorted(target for target in targets if target not in tagged_assets)
    if not unsafe_assets:
        return []
    pii_tags = sorted({tag for tags in tagged_assets.values() for tag in tags})
    return [
        Evidence(
            "pii_exposure",
            f"{asset.urn}#{field}",
            {
                "changed_field": field,
                "pii_tags": pii_tags,
                "tagged_assets": {
                    urn: tagged_assets[urn] for urn in sorted(tagged_assets)
                },
                "unsafe_assets": unsafe_assets,
                "granularity": downstream.granularity,
                "confidence": "high",
            },
        )
    ]


def _restrictions(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            value
            for value in values
            if any(word in value.lower() for word in _RESTRICTION_WORDS)
        )
    )


def _access_policy_conflicts(
    asset: TouchedAsset,
    field: str,
    source: DatasetInfo | None,
    downstream: LineageResult,
    dataset: object,
) -> list[Evidence]:
    """Compare only explicit restriction labels; absent tier metadata is not a fact."""
    if source is None:
        return []
    source_restrictions = _restrictions((*source.tags, *source.terms))
    targets = _flowing_targets(downstream)
    if not source_restrictions or not targets:
        return []

    unrestricted: dict[str, list[str]] = {}
    for target in targets:
        target_values = list(_tags(downstream, target))
        try:
            target_info = dataset(target)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 - optional enrichment cannot prove a conflict
            target_info = None
        if target_info is not None:
            target_values.extend((*target_info.tags, *target_info.terms))
        if not _restrictions(target_values):
            unrestricted[target] = sorted(set(target_values))
    if not unrestricted:
        return []
    return [
        Evidence(
            "access_policy_conflict",
            f"{asset.urn}#{field}",
            {
                "changed_field": field,
                "source_restrictions": list(source_restrictions),
                "unrestricted_targets": sorted(unrestricted),
                "target_tags": {urn: unrestricted[urn] for urn in sorted(unrestricted)},
                "granularity": downstream.granularity,
                "confidence": "high",
            },
        )
    ]
