"""Change-scoped checks that documentation still matches the post-change schema."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from sidq.gates.base import graph_unavailable
from sidq.graph.client import DatasetInfo, GraphClient
from sidq.models import Evidence, TouchedAsset

_QUOTED_REFERENCE = re.compile(r"[`\"']([^`\"']+)[`\"']")
_SNAKE_CASE_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*_[a-z0-9_]+(?![A-Za-z0-9_])"
)


class DocRotGate:
    """Check documentation supplied by the graph for only the assets being changed."""

    id = "doc_rot"

    def collect(
        self, change: Sequence[TouchedAsset], graph: GraphClient
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        documentation = getattr(graph, "get_documentation", None)
        if not callable(documentation):
            return evidence

        for asset in sorted(change, key=lambda item: item.urn):
            try:
                raw = documentation(asset.urn)
            except Exception as error:  # noqa: BLE001 - graph transports vary
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            documents = _documents(raw, asset.urn)
            if not documents:
                continue
            try:
                info = graph.get_dataset(asset.urn)
            except Exception as error:  # noqa: BLE001 - graph transports vary
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            if info is None:
                continue
            schema = _post_change_fields(info, asset)
            for subject, description in documents:
                for mentioned_field in _references(description):
                    if mentioned_field in schema:
                        continue
                    evidence.append(
                        Evidence(
                            "doc_rot",
                            subject,
                            {
                                "mentioned_field": mentioned_field,
                                "description": description,
                                "post_change_fields": sorted(schema),
                                "removed_fields": sorted(asset.removed_fields),
                                "added_fields": sorted(asset.added_fields),
                                "confidence": "high",
                            },
                        )
                    )
        return sorted(
            evidence, key=lambda item: (item.subject, item.detail["mentioned_field"])
        )


def _documents(raw: Any, asset_urn: str) -> list[tuple[str, str]]:
    if not isinstance(raw, Mapping):
        return []
    urn = raw.get("urn")
    if not isinstance(urn, str):
        urn = raw.get("asset_urn", asset_urn)
    description = raw.get("description")
    result: list[tuple[str, str]] = []
    if isinstance(urn, str) and isinstance(description, str) and description:
        result.append((urn, description))
    field_descriptions = raw.get("field_descriptions", {})
    if isinstance(urn, str) and isinstance(field_descriptions, Mapping):
        result.extend(
            (f"{urn}#{field}", description)
            for field, description in field_descriptions.items()
            if isinstance(field, str) and isinstance(description, str) and description
        )
    return sorted(result)


def _post_change_fields(info: DatasetInfo, asset: TouchedAsset) -> set[str]:
    return {
        *(field.path for field in info.fields),
        *asset.added_fields,
    }.difference(asset.removed_fields)


def _references(description: str) -> list[str]:
    quoted = {match.group(1) for match in _QUOTED_REFERENCE.finditer(description)}
    snake_case = {
        match.group(0) for match in _SNAKE_CASE_REFERENCE.finditer(description)
    }
    return sorted(quoted | snake_case)
