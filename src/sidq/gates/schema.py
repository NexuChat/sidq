"""Gate 1: referenced datasets and fields must exist in the graph."""

from __future__ import annotations

from collections.abc import Sequence

from sidq.gates.base import graph_unavailable
from sidq.graph.client import DatasetInfo, GraphClient
from sidq.models import Evidence, FieldRef, TouchedAsset


class SchemaGate:
    id = "schema"

    def collect(self, change: Sequence[TouchedAsset], graph: GraphClient) -> list[Evidence]:
        evidence: list[Evidence] = []
        cache: dict[str, DatasetInfo | None] = {}
        failed: set[str] = set()

        def dataset(urn: str) -> DatasetInfo | None:
            if urn not in cache:
                cache[urn] = graph.get_dataset(urn)
            return cache[urn]

        references = sorted(
            {reference for asset in change for reference in asset.referenced_fields},
            key=lambda reference: (reference.dataset_urn, reference.field_path),
        )
        for reference in references:
            try:
                info = dataset(reference.dataset_urn)
            except Exception as error:  # noqa: BLE001 - graph transports may raise arbitrary client errors
                if reference.dataset_urn not in failed:
                    evidence.append(graph_unavailable(reference.dataset_urn, error))
                    failed.add(reference.dataset_urn)
                continue
            if info is None:
                evidence.append(Evidence("unknown_dataset", reference.dataset_urn, {"referenced_by": _sources(change, reference)}))
                continue
            path, expected_type = _typed_path(reference.field_path)
            field = next((candidate for candidate in info.fields if candidate.path == path), None)
            if field is None:
                evidence.append(Evidence("unknown_field", f"{reference.dataset_urn}#{path}", {"field_path": path}))
            elif expected_type is not None and not _compatible(expected_type, field.native_type):
                evidence.append(
                    Evidence(
                        "type_mismatch",
                        f"{reference.dataset_urn}#{path}",
                        {"expected_type": expected_type, "graph_type": field.native_type},
                    )
                )
        return evidence


def _sources(change: Sequence[TouchedAsset], reference: FieldRef) -> list[str]:
    return sorted(asset.urn for asset in change if reference in asset.referenced_fields)


def _typed_path(path: str) -> tuple[str, str | None]:
    """Accept ``field::type`` annotations from callers that know a source type."""
    name, marker, expected = path.rpartition("::")
    return (name, expected) if marker and name and expected else (path, None)


def _compatible(expected: str, actual: str) -> bool:
    normalize = lambda value: value.strip().lower().replace(" ", "")
    aliases = {"int": "integer", "int4": "integer", "int8": "bigint", "varchar": "character varying", "bool": "boolean"}
    expected_normalized = aliases.get(normalize(expected), normalize(expected))
    actual_normalized = aliases.get(normalize(actual), normalize(actual))
    return expected_normalized == actual_normalized
