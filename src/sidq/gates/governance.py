"""Gate 3: collect governance facts the current change model can prove."""

from __future__ import annotations

from collections.abc import Sequence

from sidq.gates.base import graph_unavailable
from sidq.graph.client import DatasetInfo, GraphClient
from sidq.models import Evidence, TouchedAsset


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

            # TouchedAsset records projected columns before/after a local SQL
            # change, while GraphClient exposes only the catalog's current
            # lineage.  It carries no before/proposed edge snapshots and no
            # output-to-source-field mapping.  Combining those two views cannot
            # prove that this change introduced or expanded a governed route.
            #
            # Older code nevertheless called the current downstream graph for
            # every added or removed field.  A removed field then appeared to
            # "create" PII exposure, and an existing downstream asset tag was
            # mistaken for classification of the source field.  Route findings
            # stay disabled until a real pre/post column-edge delta is available;
            # emitting nothing is more honest than manufacturing causality.
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
