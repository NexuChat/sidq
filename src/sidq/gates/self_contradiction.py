"""Read-only checks for contradictions entirely inside a DataHub catalog.

The check deliberately works from a complete catalog snapshot.  A partial search
result is not evidence that an entity, field, owner, or tag is absent, so callers
that cannot provide a snapshot get explicit ``*_unverifiable`` evidence instead.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
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
# A column reference inside prose. Written for Unicode from the start: the
# ASCII-only form this replaced could not see a single Arabic, Chinese,
# Japanese, or Cyrillic column name, so doc rot in any non-English catalog was
# invisible — the check ran and found nothing, which is the exact failure this
# project exists to catch. `\w` under Python's default Unicode semantics covers
# every script; the underscore requirement stays, because it is what
# distinguishes a column name from an ordinary word in a sentence.
_SNAKE_CASE = r"\w+_\w+"
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
    field_lineage_resolved: frozenset[str] | None = None
    """Assets whose *column-level* lineage was actually fetched.

    ``None`` means every asset's field lineage is present, which is true of the SDK
    path because one aspect read returns the whole fine-grained record. The MCP
    path has to ask per column, so it can only afford a subset — and the assets it
    could not afford must be reported as unexamined rather than passed over in
    silence, or a bounded read would masquerade as a clean bill of health.
    """

    @classmethod
    def from_mcp(
        cls,
        graph: Any,
        *,
        query: str = "*",
        depth: int = 2,
        field_lineage_budget: int = 0,
    ) -> CatalogSnapshot:
        """Read the snapshot through the official DataHub MCP server.

        `from_datahub` below reads through the DataHub Python SDK, which is a
        legitimate client but not an MCP one — and an audit that claims to run on
        the official agent surface should actually run on it. This path uses only
        `search`, `get_entities`, `list_schema_fields`, and `get_lineage`, which is
        exactly what `mcp-server-datahub` exposes read-only.

        It is deliberately narrower than the SDK path, in two measured ways.

        MCP `search` is paginated, so this sees one bounded page rather than the
        whole catalog — what an agent would actually see. And MCP `get_lineage`
        returns column granularity only when asked for one named column, so field
        lineage costs one call per column (0.116s each against the local DataHub on
        2026-07-30). `field_lineage_budget` caps how many assets are worth that:
        the most connected ones first, since a wrong claim about a widely consumed
        asset is the one that propagates. Everything past the cap is recorded as
        unresolved, never as clean.
        """
        search = getattr(graph, "search_assets", None) or getattr(
            graph, "_search", None
        )
        if not callable(search) or not callable(getattr(graph, "get_dataset", None)):
            raise _Unverifiable("graph does not expose the MCP read surface")

        entities: list[CatalogEntity] = []
        table_edges: dict[str, list[LineageEdge]] = {}
        # DataHub reports column-level PII by glossary term, and `list_schema_fields`
        # returns the term's display name while every write tool needs its URN. The
        # dataset-level payload carries both, so the catalog resolves its own names.
        glossary: dict[str, str] = {}
        for urn in _mcp_urns(search, query):
            dataset = graph.get_dataset(urn)
            if dataset is None:
                continue
            glossary.update(dict(getattr(dataset, "glossary", ()) or ()))
            entities.append(
                CatalogEntity(
                    urn=urn,
                    kind="dataset",
                    fields=tuple(
                        CatalogField(
                            field.path,
                            getattr(field, "description", None),
                            tuple(
                                sorted(
                                    set(getattr(field, "tags", ()))
                                    | set(getattr(field, "terms", ()))
                                )
                            ),
                        )
                        for field in dataset.fields
                    ),
                    tags=tuple(dataset.tags),
                    owners=tuple(dataset.owners),
                    deprecated=bool(dataset.deprecated),
                )
            )
            table_edges[urn] = _mcp_table_edges(graph, urn, depth)
        entities = [_with_resolved_markers(entity, glossary) for entity in entities]

        # Most-consumed first: the same ordering the auditor uses, so the assets
        # whose field lineage gets resolved are the ones it will examine.
        ranked = sorted(
            (entity.urn for entity in entities),
            key=lambda urn: (-len(table_edges.get(urn, ())), urn),
        )
        affordable = set(ranked[: max(field_lineage_budget, 0)])
        by_urn = {entity.urn: entity for entity in entities}
        resolved: set[str] = set()
        edges: list[LineageEdge] = []
        for urn, table_only in table_edges.items():
            if urn not in affordable:
                edges.extend(table_only)
                continue
            try:
                field_edges = _mcp_field_edges(graph, by_urn[urn], depth)
            except _Unverifiable:
                # Budgeted for, but not actually read: keep the table-level view
                # and leave the asset out of `resolved` so it is reported as such.
                edges.extend(table_only)
                continue
            resolved.add(urn)
            edges.extend(field_edges or table_only)
        return cls(
            tuple(sorted(entities, key=lambda item: item.urn)),
            tuple(sorted(edges, key=_edge_key)),
            frozenset(resolved),
        )

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
        evidence.extend(_field_lineage_unresolved(entities, snapshot))
        return evidence


class _Unverifiable(RuntimeError):
    pass


def _mcp_urns(search: Any, query: str) -> list[str]:
    """Pull dataset URNs out of whatever shape the MCP search tool returns."""
    raw = search(query)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return sorted(dict.fromkeys(raw))
    found: list[str] = []
    stack: list[Any] = [raw]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            urn = item.get("urn")
            if isinstance(urn, str) and urn.startswith("urn:li:dataset:"):
                found.append(urn)
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return sorted(dict.fromkeys(found))


def _with_resolved_markers(
    entity: CatalogEntity, glossary: Mapping[str, str]
) -> CatalogEntity:
    """Replace a column marker's display name with the URN the catalog uses for it.

    Detection works on either form, so this changes no finding. It exists so a
    repair can be *written*: `add_terms` takes term URNs, and a repair agent that
    proposed the string "PII" would produce a call the server rejects. When the
    catalog never names the URN, the display name is kept and the repair declines
    to write rather than guessing one.
    """
    if not glossary:
        return entity
    return replace(
        entity,
        fields=tuple(
            replace(
                item, tags=tuple(sorted({glossary.get(tag, tag) for tag in item.tags}))
            )
            for item in entity.fields
        ),
    )


def _mcp_table_edges(graph: Any, urn: str, depth: int) -> list[LineageEdge]:
    """Table-granularity downstream edges for one asset, over the MCP lineage tool."""
    try:
        result = graph.get_downstream(urn, depth)
    except Exception:  # noqa: BLE001 - a lineage gap is absence, not a crash
        return []
    targets = getattr(result, "urns", ()) if result is not None else ()
    return [LineageEdge(urn, None, str(target), None) for target in targets or ()]


def _mcp_field_edges(
    graph: Any, entity: CatalogEntity, depth: int
) -> list[LineageEdge]:
    """Column-granularity edges for one asset: one MCP lineage call per column.

    Raises `_Unverifiable` if any single column cannot be read. Partial success is
    the dangerous case here — the asset would be marked resolved while a column
    nobody managed to ask about stayed silent, which is precisely how an
    unperformed check turns into a clean one.
    """
    edges: list[LineageEdge] = []
    for field in entity.fields:
        try:
            result = graph.get_downstream(entity.urn, depth, field.path)
        except Exception as error:
            raise _Unverifiable(f"column lineage failed for {field.path}") from error
        columns = getattr(result, "columns", {}) if result is not None else {}
        if not isinstance(columns, Mapping):
            raise _Unverifiable(f"column lineage was unreadable for {field.path}")
        for target, target_fields in columns.items():
            edges.extend(
                LineageEdge(entity.urn, field.path, str(target), str(target_field))
                for target_field in target_fields or ()
            )
    return edges


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


def _field_lineage_unresolved(
    entities: dict[str, CatalogEntity], snapshot: CatalogSnapshot
) -> list[Evidence]:
    """Name the assets whose column lineage was never fetched.

    Without this the bounded MCP read would look identical to a clean one: no
    field-level edge, therefore no `lineage_field_missing`, therefore apparently
    fine. Saying so per asset is what lets the auditor count them as unverifiable
    instead of counting them as verified.
    """
    resolved = snapshot.field_lineage_resolved
    if resolved is None:
        return []
    return [
        _unverifiable(
            "lineage_field_missing",
            "column lineage was not fetched for this asset (field-lineage budget)",
            urn,
        )
        for urn, entity in sorted(entities.items())
        if entity.kind == "dataset" and entity.fields and urn not in resolved
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
