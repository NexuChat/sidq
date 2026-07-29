"""Read-only checks for contradictions entirely inside a DataHub catalog.

The check deliberately works from a complete catalog snapshot.  A partial search
result is not evidence that an entity, field, owner, or tag is absent, so callers
that cannot provide a snapshot get explicit ``*_unverifiable`` evidence instead.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sidq.models import Evidence, TouchedAsset

_CHECKS = (
    "deprecated_upstream_of_live",
    "doc_references_missing_column",
    "lineage_field_missing",
    "orphan_lineage",
    "pii_leak_untagged",
    "unowned_consumed",
)
_FIELD_URN = re.compile(
    r"^urn:li:schemaField:\((urn:li:dataset:\(urn:li:dataPlatform:[^,]+,[^,]+,[^)]+\)),(.+)\)$"
)
_SNAKE_CASE = r"[a-z][a-z0-9]*_[a-z0-9_]+"
_COLUMN_REFERENCE = re.compile(
    rf"\b(?:column|field)\s+[`\"']?({_SNAKE_CASE})[`\"']?\b", re.IGNORECASE
)
_TRAILING_COLUMN_REFERENCE = re.compile(
    rf"[`\"']?({_SNAKE_CASE})[`\"']?\s+\b(?:column|field)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class CatalogField:
    path: str
    description: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogEntity:
    urn: str
    kind: str
    description: str | None = None
    fields: tuple[CatalogField, ...] = ()
    tags: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    deprecated: bool = False
    live: bool = True
    schema_available: bool = True


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """A catalog lineage assertion, optionally at field granularity."""

    source_urn: str
    source_field: str | None
    target_urn: str
    target_field: str | None


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    """The complete read-only metadata needed to audit a catalog."""

    entities: tuple[CatalogEntity, ...]
    edges: tuple[LineageEdge, ...] = ()

    @classmethod
    def from_datahub(cls, graph: Any) -> CatalogSnapshot:
        """Read a complete DataHub snapshot through the supported Python client.

        This is duck-typed on purpose: it keeps the normal GraphClient seam
        unchanged while allowing the gate to run directly against any DataHub
        SDK client that supplies ``list_all_entity_urns`` and ``get_aspect``.
        No source system or mutation API is used.
        """
        list_urns = getattr(graph, "list_all_entity_urns", None)
        get_aspect = getattr(graph, "get_aspect", None)
        if not callable(list_urns) or not callable(get_aspect):
            raise _Unverifiable("graph does not expose a complete catalog snapshot")

        from datahub.metadata.schema_classes import (
            ChartInfoClass,
            DashboardInfoClass,
            DatasetPropertiesClass,
            GlobalTagsClass,
            OwnershipClass,
            SchemaMetadataClass,
            StatusClass,
            UpstreamLineageClass,
        )

        urns_by_kind = {
            kind: tuple(_all_entity_urns(list_urns, kind))
            for kind in ("dataset", "chart", "dashboard")
        }
        entities: list[CatalogEntity] = []
        edges: set[LineageEdge] = set()
        for kind, urns in urns_by_kind.items():
            for urn in urns:
                properties = (
                    get_aspect(urn, DatasetPropertiesClass)
                    if kind == "dataset"
                    else None
                )
                ownership = get_aspect(urn, OwnershipClass)
                status = get_aspect(urn, StatusClass)
                tags = get_aspect(urn, GlobalTagsClass)
                schema = (
                    get_aspect(urn, SchemaMetadataClass) if kind == "dataset" else None
                )
                fields = _fields(schema)
                entities.append(
                    CatalogEntity(
                        urn=urn,
                        kind=kind,
                        description=_text(properties, "description"),
                        fields=fields,
                        tags=_tags(tags),
                        owners=_owners(ownership),
                        deprecated=_deprecated(properties),
                        live=not bool(_value(status, "removed", False)),
                        schema_available=schema is not None
                        if kind == "dataset"
                        else False,
                    )
                )
                if kind == "dataset":
                    lineage = get_aspect(urn, UpstreamLineageClass)
                    edges.update(_lineage_edges(urn, lineage))
                elif kind == "chart":
                    chart = get_aspect(urn, ChartInfoClass)
                    for input_urn in _urn_list(chart, "inputs"):
                        edges.add(LineageEdge(input_urn, None, urn, None))
                elif kind == "dashboard":
                    dashboard = get_aspect(urn, DashboardInfoClass)
                    for chart_urn in _urn_list(dashboard, "charts"):
                        edges.add(LineageEdge(chart_urn, None, urn, None))
        return cls(
            tuple(sorted(entities, key=lambda item: item.urn)),
            tuple(sorted(edges, key=_edge_key)),
        )


class SelfContradictionGate:
    """Find catalog claims that disagree with other catalog claims only."""

    id = "graph_self_contradiction"

    def collect(self, change: Sequence[TouchedAsset], graph: Any) -> list[Evidence]:
        del change  # This audit examines the supplied complete graph, not a diff.
        try:
            snapshot = _snapshot(graph)
        except _Unverifiable as error:
            return [_unverifiable(check, str(error)) for check in _CHECKS]
        except Exception as error:  # noqa: BLE001 - catalog transports have varied error types
            return [
                _unverifiable(
                    check, f"catalog snapshot could not be read: {type(error).__name__}"
                )
                for check in _CHECKS
            ]

        entities = {entity.urn: entity for entity in snapshot.entities}
        evidence: list[Evidence] = []
        evidence.extend(_deprecated_upstream_of_live(entities, snapshot.edges))
        evidence.extend(_documentation(entities))
        evidence.extend(_lineage_field_missing(entities, snapshot.edges))
        evidence.extend(_orphan_lineage(entities, snapshot.edges))
        evidence.extend(_pii_leaks(entities, snapshot.edges))
        evidence.extend(_unowned_consumed(entities, snapshot.edges))
        return evidence


class _Unverifiable(RuntimeError):
    pass


def _snapshot(graph: Any) -> CatalogSnapshot:
    supplied = getattr(graph, "catalog_snapshot", None)
    if callable(supplied):
        snapshot = supplied()
        if isinstance(snapshot, CatalogSnapshot):
            return snapshot
        raise _Unverifiable("catalog_snapshot did not return CatalogSnapshot")
    return CatalogSnapshot.from_datahub(graph)


def _documentation(entities: dict[str, CatalogEntity]) -> list[Evidence]:
    evidence: list[Evidence] = []
    for entity in sorted(entities.values(), key=lambda item: item.urn):
        if entity.kind != "dataset" or not entity.schema_available:
            continue
        schema = {field.path for field in entity.fields}
        for field in sorted(entity.fields, key=lambda item: item.path):
            evidence.extend(
                _documentation_claims(
                    entity, schema, field.description, f"{entity.urn}#{field.path}"
                )
            )
        evidence.extend(
            _documentation_claims(entity, schema, entity.description, entity.urn)
        )
    return evidence


def _documentation_claims(
    entity: CatalogEntity, schema: set[str], description: str | None, subject: str
) -> list[Evidence]:
    if not description:
        return []
    # A quoted or bare snake_case word is still often a table, schema, file,
    # value, or config key.  Require the adjacent word "column" or "field".
    # This is a strict subset of possible documentation references by design.
    references = sorted(
        {match.group(1) for match in _COLUMN_REFERENCE.finditer(description)}
        | {match.group(1) for match in _TRAILING_COLUMN_REFERENCE.finditer(description)}
    )
    return [
        Evidence(
            "doc_references_missing_column",
            subject,
            {
                "mentioned_column": reference,
                "description": description,
                "schema_fields": sorted(schema),
                "confidence": "high",
            },
        )
        for reference in references
        if reference not in schema
    ]


def _lineage_field_missing(
    entities: dict[str, CatalogEntity], edges: Iterable[LineageEdge]
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for edge in sorted(edges, key=_edge_key):
        if edge.target_field is None:
            continue
        target = entities.get(edge.target_urn)
        if target is None or target.kind != "dataset":
            continue  # orphan_lineage is the only confident statement in this case.
        if not target.schema_available:
            evidence.append(
                _unverifiable(
                    "lineage_field_missing",
                    "target schema is unavailable",
                    edge.target_urn,
                )
            )
            continue
        fields = {field.path for field in target.fields}
        if edge.target_field not in fields:
            evidence.append(
                Evidence(
                    "lineage_field_missing",
                    f"{edge.target_urn}#{edge.target_field}",
                    {
                        "edge": _edge_data(edge),
                        "target_schema_fields": sorted(fields),
                        "confidence": "high",
                    },
                )
            )
    return _unique(evidence)


def _orphan_lineage(
    entities: dict[str, CatalogEntity], edges: Iterable[LineageEdge]
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for edge in sorted(edges, key=_edge_key):
        for side, urn in (("source", edge.source_urn), ("target", edge.target_urn)):
            if urn not in entities:
                evidence.append(
                    Evidence(
                        "orphan_lineage",
                        urn,
                        {
                            "edge": _edge_data(edge),
                            "missing_endpoint": side,
                            "confidence": "high",
                        },
                    )
                )
    return _unique(evidence)


def _pii_leaks(
    entities: dict[str, CatalogEntity], edges: Iterable[LineageEdge]
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for edge in sorted(edges, key=_edge_key):
        if edge.source_field is None or edge.target_field is None:
            continue
        source = entities.get(edge.source_urn)
        target = entities.get(edge.target_urn)
        if (
            source is None
            or target is None
            or source.kind != "dataset"
            or target.kind != "dataset"
        ):
            continue
        if not source.schema_available or not target.schema_available:
            evidence.append(
                _unverifiable(
                    "pii_leak_untagged",
                    "source or target schema is unavailable",
                    edge.target_urn,
                )
            )
            continue
        source_field = _field(source, edge.source_field)
        target_field = _field(target, edge.target_field)
        if source_field is None or target_field is None:
            continue  # Missing fields are handled by lineage_field_missing, not inferred PII.
        pii_tags = tuple(sorted(tag for tag in source_field.tags if _is_pii_tag(tag)))
        if not pii_tags:
            continue
        target_tags = tuple(sorted(target_field.tags))
        if not set(pii_tags).intersection(target_tags):
            evidence.append(
                Evidence(
                    "pii_leak_untagged",
                    f"{target.urn}#{target_field.path}",
                    {
                        "edge": _edge_data(edge),
                        "source_pii_tags": list(pii_tags),
                        "target_tags": list(target_tags),
                        "confidence": "high",
                    },
                )
            )
    return _unique(evidence)


def _deprecated_upstream_of_live(
    entities: dict[str, CatalogEntity], edges: Iterable[LineageEdge]
) -> list[Evidence]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.source_urn in entities and edge.target_urn in entities:
            adjacency[edge.source_urn].add(edge.target_urn)
    evidence: list[Evidence] = []
    for source in sorted(entities.values(), key=lambda item: item.urn):
        if not source.deprecated:
            continue
        for consumer, path in _live_bi_consumers(source.urn, adjacency, entities):
            evidence.append(
                Evidence(
                    "deprecated_upstream_of_live",
                    source.urn,
                    {
                        "deprecated_asset": source.urn,
                        "live_consumer": consumer.urn,
                        "path": path,
                        "confidence": "high",
                    },
                )
            )
    return evidence


def _live_bi_consumers(
    source: str, adjacency: dict[str, set[str]], entities: dict[str, CatalogEntity]
) -> Iterable[tuple[CatalogEntity, list[str]]]:
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(source, (source,))])
    visited = {source}
    while queue:
        current, path = queue.popleft()
        for next_urn in sorted(adjacency.get(current, ())):
            if next_urn in visited:
                continue
            visited.add(next_urn)
            next_path = (*path, next_urn)
            entity = entities[next_urn]
            if entity.kind in {"chart", "dashboard"} and entity.live:
                yield entity, list(next_path)
            queue.append((next_urn, next_path))


def _unowned_consumed(
    entities: dict[str, CatalogEntity], edges: Iterable[LineageEdge]
) -> list[Evidence]:
    consumers: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if (
            edge.source_urn in entities
            and edge.target_urn in entities
            and edge.source_urn != edge.target_urn
        ):
            consumers[edge.source_urn].add(edge.target_urn)
    return [
        Evidence(
            "unowned_consumed",
            entity.urn,
            {
                "downstream_consumers": sorted(consumers[entity.urn]),
                "confidence": "high",
            },
        )
        for entity in sorted(entities.values(), key=lambda item: item.urn)
        if consumers[entity.urn] and not entity.owners
    ]


def _unverifiable(check: str, reason: str, subject: str = "catalog") -> Evidence:
    return Evidence(
        f"{check}_unverifiable", subject, {"reason": reason, "confidence": "none"}
    )


def _unique(evidence: Iterable[Evidence]) -> list[Evidence]:
    result: list[Evidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        key = (item.kind, item.subject, repr(sorted(item.detail.items())))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _edge_key(edge: LineageEdge) -> tuple[str, str, str, str]:
    return (
        edge.source_urn,
        edge.source_field or "",
        edge.target_urn,
        edge.target_field or "",
    )


def _edge_data(edge: LineageEdge) -> dict[str, str | None]:
    return {
        "source_dataset": edge.source_urn,
        "source_field": edge.source_field,
        "target_dataset": edge.target_urn,
        "target_field": edge.target_field,
    }


def _field(entity: CatalogEntity, path: str) -> CatalogField | None:
    return next((field for field in entity.fields if field.path == path), None)


def _is_pii_tag(tag: str) -> bool:
    normalized = tag.casefold()
    return "pii" in normalized or "personally_identifiable" in normalized


def _all_entity_urns(list_urns: Any, kind: str) -> Iterable[str]:
    start = 0
    while True:
        page = list_urns(kind, start, 500)
        if not page:
            return
        urns = [urn for urn in page if isinstance(urn, str)]
        yield from urns
        if len(urns) < 500:
            return
        start += len(urns)


def _lineage_edges(target_urn: str, lineage: Any) -> Iterable[LineageEdge]:
    if lineage is None:
        return ()
    edges: list[LineageEdge] = []
    for upstream in _value(lineage, "upstreams", ()) or ():
        source = _text(upstream, "dataset")
        if source:
            edges.append(LineageEdge(source, None, target_urn, None))
    for record in _value(lineage, "fineGrainedLineages", ()) or ():
        sources = [
            field
            for urn in _value(record, "upstreams", ()) or ()
            if (field := _schema_field(urn))
        ]
        targets = [
            field
            for urn in _value(record, "downstreams", ()) or ()
            if (field := _schema_field(urn))
        ]
        for source_urn, source_field in sources:
            for target, target_field in targets:
                edges.append(
                    LineageEdge(source_urn, source_field, target, target_field)
                )
    return edges


def _schema_field(urn: Any) -> tuple[str, str] | None:
    if not isinstance(urn, str):
        return None
    match = _FIELD_URN.match(urn)
    return (match.group(1), match.group(2)) if match else None


def _fields(schema: Any) -> tuple[CatalogField, ...]:
    if schema is None:
        return ()
    fields: list[CatalogField] = []
    for field in _value(schema, "fields", ()) or ():
        path = _text(field, "fieldPath")
        if path:
            fields.append(
                CatalogField(
                    path,
                    _text(field, "description"),
                    _tags(_value(field, "globalTags", None)),
                )
            )
    return tuple(sorted(fields, key=lambda item: item.path))


def _tags(value: Any) -> tuple[str, ...]:
    tags = _value(value, "tags", ()) or ()
    result: set[str] = set()
    for item in tags:
        tag = _value(item, "tag", item)
        urn = tag if isinstance(tag, str) else _text(tag, "urn")
        if urn:
            result.add(urn)
    return tuple(sorted(result))


def _owners(value: Any) -> tuple[str, ...]:
    owners = _value(value, "owners", ()) or ()
    return tuple(sorted({owner for item in owners if (owner := _text(item, "owner"))}))


def _urn_list(value: Any, key: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                urn
                for item in _value(value, key, ()) or ()
                if (
                    urn := _text(item, "urn") or item if isinstance(item, str) else None
                )
            }
        )
    )


def _deprecated(properties: Any) -> bool:
    custom = _value(properties, "customProperties", {}) or {}
    value = custom.get("deprecated") if isinstance(custom, dict) else None
    return str(value).casefold() in {"true", "yes", "deprecated"}


def _text(value: Any, key: str) -> str | None:
    candidate = _value(value, key, None)
    return candidate if isinstance(candidate, str) else None


def _value(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
