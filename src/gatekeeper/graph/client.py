"""A transport-independent view of the small slice of DataHub gates need."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SchemaField:
    """A source or catalog field, deliberately independent of DataHub aspects."""

    path: str
    native_type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    urn: str
    fields: tuple[SchemaField, ...] = ()
    tags: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    deprecated: bool = False


@dataclass(frozen=True, slots=True)
class LineagePath:
    """One directed lineage path, represented as URNs so it is JSON-friendly."""

    urns: tuple[str, ...]
    granularity: str = "table"


# Kept as an alias because the engine specification calls this domain object Path.
type Path = LineagePath


@dataclass(frozen=True, slots=True)
class LineageResult:
    urns: tuple[str, ...] = ()
    entity_types: Mapping[str, str] | tuple[str, ...] = ()
    paths: tuple[LineagePath, ...] = ()
    granularity: str = "table"


@runtime_checkable
class GraphClient(Protocol):
    """The narrow graph seam. Gates never know that its implementation uses MCP."""

    def get_dataset(self, urn: str) -> DatasetInfo | None: ...

    def find_dataset(self, name_or_urn: str) -> str | None: ...

    def get_downstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult: ...

    def paths_between(self, a: str, b: str) -> list[Path]: ...


type ToolCaller = Callable[[str, Mapping[str, Any]], Any]


class MCPGraphClient:
    """Translate official DataHub MCP tools into the stable :class:`GraphClient` API.

    ``tool_caller`` is intentionally a tiny synchronous boundary. Production code can
    bridge an MCP session there, while tests can inject a regular function. Every
    official-tool invocation lives in one of the five helpers below.
    """

    def __init__(self, tool_caller: ToolCaller) -> None:
        self._tool_caller = tool_caller

    # ASSUMED-SIGNATURE: mcp-server-datahub v0.6.0 search(query=..., entity_types=[...]).
    def _search(self, query: str) -> Any:
        return self._tool_caller("search", {"query": query, "entity_types": ["dataset"]})

    # ASSUMED-SIGNATURE: mcp-server-datahub v0.6.0 get_entities(urns=[...]).
    def _get_entities(self, urns: Sequence[str]) -> Any:
        return self._tool_caller("get_entities", {"urns": list(urns)})

    # ASSUMED-SIGNATURE: mcp-server-datahub v0.6.0 list_schema_fields(urn=...).
    def _list_schema_fields(self, urn: str) -> Any:
        return self._tool_caller("list_schema_fields", {"urn": urn})

    # ASSUMED-SIGNATURE: mcp-server-datahub v0.6.0 get_lineage(urn=..., direction="downstream", depth=..., column=...).
    def _get_lineage(self, urn: str, depth: int, column: str | None) -> Any:
        arguments: dict[str, Any] = {"urn": urn, "direction": "downstream", "depth": depth}
        if column is not None:
            arguments["column"] = column
        return self._tool_caller("get_lineage", arguments)

    # ASSUMED-SIGNATURE: mcp-server-datahub v0.6.0 get_lineage_paths_between(source_urn=..., destination_urn=...).
    def _get_lineage_paths_between(self, a: str, b: str) -> Any:
        return self._tool_caller(
            "get_lineage_paths_between", {"source_urn": a, "destination_urn": b}
        )

    def find_dataset(self, name_or_urn: str) -> str | None:
        if name_or_urn.startswith("urn:li:dataset:"):
            return name_or_urn if self.get_dataset(name_or_urn) is not None else None
        for item in _items(self._search(name_or_urn)):
            urn = _string(item, "urn", "entity_urn", "entityUrn")
            if urn and "dataset" in urn:
                return urn
        return None

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        entity = _first_entity(self._get_entities([urn]), urn)
        if entity is None:
            return None
        return DatasetInfo(
            urn=urn,
            fields=tuple(_parse_fields(self._list_schema_fields(urn))),
            tags=tuple(sorted(set(_strings(entity, "tags", "tag_urns", "tagUrns")))),
            terms=tuple(sorted(set(_strings(entity, "terms", "term_urns", "termUrns")))),
            owners=tuple(sorted(set(_strings(entity, "owners", "owner_urns", "ownerUrns")))),
            deprecated=_deprecated(entity),
        )

    def get_downstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult:
        raw = self._get_lineage(urn, depth, column)
        return _parse_lineage(raw, requested_column=column)

    def paths_between(self, a: str, b: str) -> list[Path]:
        return list(_parse_paths(self._get_lineage_paths_between(a, b)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [_mapping(item) for item in value]
    document = _mapping(value)
    for key in ("results", "entities", "items", "data", "schema_fields", "fields", "paths"):
        nested = document.get(key)
        if isinstance(nested, list):
            return [_mapping(item) for item in nested]
    return []


def _string(value: Any, *keys: str) -> str | None:
    document = _mapping(value)
    for key in keys:
        candidate = document.get(key)
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, Mapping):
            nested = _string(candidate, "urn", "id")
            if nested:
                return nested
    return None


def _strings(value: Any, *keys: str) -> list[str]:
    document = _mapping(value)
    for key in keys:
        candidate = document.get(key)
        if isinstance(candidate, list):
            values: list[str] = []
            for item in candidate:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, Mapping):
                    urn = _string(item, "urn", "id", "name")
                    if urn:
                        values.append(urn)
            return values
    return []


def _first_entity(value: Any, urn: str) -> Mapping[str, Any] | None:
    document = _mapping(value)
    if urn in document and isinstance(document[urn], Mapping):
        return _mapping(document[urn])
    for entity in _items(value):
        if _string(entity, "urn", "entity_urn", "entityUrn") in (None, urn):
            return entity
    return None


def _parse_fields(value: Any) -> list[SchemaField]:
    fields: list[SchemaField] = []
    for field in _items(value):
        path = _string(field, "path", "field_path", "fieldPath", "name")
        if not path:
            continue
        native_type = _string(field, "native_type", "nativeType", "type", "data_type", "dataType") or ""
        nullable_value = field.get("nullable", field.get("is_nullable", field.get("isNullable", True)))
        nullable = nullable_value if isinstance(nullable_value, bool) else str(nullable_value).lower() in {"true", "yes", "y", "1"}
        fields.append(SchemaField(path, native_type, nullable))
    return sorted(fields, key=lambda field: field.path)


def _deprecated(value: Mapping[str, Any]) -> bool:
    candidate = value.get("deprecated", value.get("deprecation"))
    if isinstance(candidate, Mapping):
        candidate = candidate.get("deprecated", candidate.get("is_deprecated", True))
    return candidate is True or str(candidate).lower() in {"true", "yes", "deprecated"}


def _parse_lineage(value: Any, *, requested_column: str | None) -> LineageResult:
    document = _mapping(value)
    items = _items(value)
    urns: list[str] = []
    entity_types: dict[str, str] = {}
    for item in items:
        urn = _string(item, "urn", "entity_urn", "entityUrn", "destination_urn", "destinationUrn")
        if urn:
            urns.append(urn)
            entity_type = _string(item, "entity_type", "entityType", "type")
            if entity_type:
                entity_types[urn] = entity_type
    granularity = str(document.get("granularity", "column" if requested_column and document.get("column_lineage") else "table"))
    return LineageResult(
        urns=tuple(sorted(set(urns))),
        entity_types=entity_types,
        paths=tuple(_parse_paths(document.get("paths", []))),
        granularity=granularity if granularity in {"column", "table"} else "table",
    )


def _parse_paths(value: Any) -> list[LineagePath]:
    paths: list[LineagePath] = []
    for item in _items(value):
        raw_nodes = item.get("urns", item.get("path", item.get("nodes", ())))
        urns = tuple(
            node if isinstance(node, str) else _string(node, "urn", "entity_urn", "entityUrn") or ""
            for node in raw_nodes
        ) if isinstance(raw_nodes, list) else ()
        urns = tuple(node for node in urns if node)
        if urns:
            granularity = str(item.get("granularity", "table"))
            paths.append(LineagePath(urns, granularity if granularity in {"column", "table"} else "table"))
    return sorted(paths, key=lambda path: (path.urns, path.granularity))
