"""A transport-independent view of the small slice of DataHub gates need."""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SchemaField:
    """A source or catalog field, deliberately independent of DataHub aspects."""

    path: str
    native_type: str
    nullable: bool
    description: str | None = None
    # Governance markers carried at *column* granularity. DataHub's showcase sample
    # marks PII with glossary terms rather than tags, and the two are different
    # write surfaces (`add_terms` vs `add_tags`), so they are kept apart here
    # instead of being flattened into one bag a repair could not act on.
    tags: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    urn: str
    fields: tuple[SchemaField, ...] = ()
    tags: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    deprecated: bool = False
    # (display name, term URN) for every glossary term on this asset. Field-level
    # markers arrive as names; writing one back requires its URN.
    glossary: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class LineagePath:
    """One directed lineage path, represented as URNs so it is JSON-friendly."""

    urns: tuple[str, ...]
    granularity: str = "table"
    note: str | None = None


# Kept as an alias because the engine specification calls this domain object Path.
type Path = LineagePath


@dataclass(frozen=True, slots=True)
class LineageResult:
    urns: tuple[str, ...] = ()
    entity_types: Mapping[str, str] | tuple[str, ...] = ()
    paths: tuple[LineagePath, ...] = ()
    columns: Mapping[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    tags: Mapping[str, tuple[str, ...]] = dataclass_field(default_factory=dict)
    granularity: str = "table"
    total: int | None = None
    returned: int | None = None
    complete: bool = True


@runtime_checkable
class GraphClient(Protocol):
    """The narrow graph seam. Gates never know that its implementation uses MCP."""

    def get_dataset(self, urn: str) -> DatasetInfo | None: ...

    def find_dataset(self, name_or_urn: str) -> str | None: ...

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult: ...

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> list[Path]: ...


type ToolCaller = Callable[[str, Mapping[str, Any]], Any]


class GraphResponseError(RuntimeError):
    """An MCP tool returned a payload that cannot prove the requested result."""


class MCPGraphClient:
    """Translate official DataHub MCP tools into the stable :class:`GraphClient` API.

    ``tool_caller`` is intentionally a tiny synchronous boundary. Production code can
    bridge an MCP session there, while tests can inject a regular function. Every
    official-tool invocation lives in one of the five helpers below.
    """

    def __init__(self, tool_caller: ToolCaller) -> None:
        self._tool_caller = tool_caller
        self._datasets: dict[str, DatasetInfo | None] = {}
        self._downstreams: dict[tuple[str, int, str | None], LineageResult] = {}

    def _search(self, query: str) -> Any:
        # Verified against mcp-server-datahub 0.6.0 on 2026-07-28.
        return self._tool_caller(
            "search",
            {"query": query, "filter": "entity_type = dataset", "num_results": 50},
        )

    def _get_entities(self, urns: Sequence[str]) -> Any:
        return self._tool_caller("get_entities", {"urns": list(urns)})

    def _list_schema_fields(self, urn: str) -> Any:
        return self._tool_caller("list_schema_fields", {"urn": urn, "limit": 100})

    def _get_lineage(self, urn: str, depth: int, column: str | None) -> Any:
        arguments: dict[str, Any] = {
            "urn": urn,
            "upstream": False,
            "max_hops": depth,
            "max_results": 100,
        }
        if column is not None:
            arguments["column"] = column
        return self._tool_caller("get_lineage", arguments)

    def _get_lineage_paths_between(
        self, a: str, b: str, source_column: str | None, target_column: str | None
    ) -> Any:
        arguments: dict[str, Any] = {
            "source_urn": a,
            "target_urn": b,
            "direction": "downstream",
        }
        if source_column is not None and target_column is not None:
            arguments["source_column"] = source_column
            arguments["target_column"] = target_column
        return self._tool_caller("get_lineage_paths_between", arguments)

    def find_dataset(self, name_or_urn: str) -> str | None:
        if name_or_urn.startswith("urn:li:dataset:"):
            return name_or_urn if self.get_dataset(name_or_urn) is not None else None
        for item in _items(self._search(name_or_urn)):
            # Live `search` results wrap the matching entity under `entity`.
            urn = _string(
                _mapping(item.get("entity")), "urn", "entity_urn", "entityUrn"
            )
            if urn and "dataset" in urn:
                return urn
        return None

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        if urn in self._datasets:
            return self._datasets[urn]
        entity = _first_entity(self._get_entities([urn]), urn)
        if entity is None:
            self._datasets[urn] = None
            return None
        result = DatasetInfo(
            urn=urn,
            fields=tuple(_parse_fields(self._list_schema_fields(urn))),
            tags=tuple(sorted(set(_strings(entity, "tags", "tag_urns", "tagUrns")))),
            terms=tuple(
                sorted(
                    set(
                        _strings(
                            entity, "glossaryTerms", "terms", "term_urns", "termUrns"
                        )
                    )
                )
            ),
            owners=tuple(sorted(set(_owners(entity)))),
            deprecated=_deprecated(entity),
            glossary=_glossary(entity),
        )
        self._datasets[urn] = result
        return result

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        key = (urn, depth, column)
        if key in self._downstreams:
            return self._downstreams[key]
        raw = self._get_lineage(urn, depth, column)
        result = _parse_lineage(raw, requested_column=column)
        self._downstreams[key] = result
        return result

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> list[Path]:
        raw = self._get_lineage_paths_between(a, b, source_column, target_column)
        document = _required_mapping(raw, "get_lineage_paths_between response")
        paths = document.get("paths")
        if not isinstance(paths, list):
            raise GraphResponseError(
                "get_lineage_paths_between response is missing a paths list"
            )
        metadata = _mapping(document.get("metadata"))
        default_granularity = (
            "column" if metadata.get("pathType") == "column-level" else "table"
        )
        return _downstream_paths(
            _parse_paths({"paths": paths}, default_granularity=default_granularity),
            a,
            b,
            source_column=source_column,
            target_column=target_column,
        )

    def close(self) -> None:
        close = getattr(self._tool_caller, "close", None)
        if callable(close):
            close()


class StdioMCPToolCaller:
    """Synchronous bridge over a real stdio MCP ClientSession.

    The graph seam is synchronous, while the official MCP client is async. This
    owns one background event loop and one ClientSession for an entire check, so a
    blast traversal does not restart the MCP server for every graph hop.
    """

    def __init__(
        self, command: str | None = None, *, gms_url: str | None = None
    ) -> None:
        # Resolved from PATH, exactly like the receipt writer's caller. The old
        # default pointed into this repository's own venv, which assumed the
        # server can share the client's environment — it cannot: the client
        # needs mcp>=2 while the server's fastmcp still imports the mcp 1.x
        # internals, so a same-venv install crashes on startup. The server runs
        # from its own isolated install (`uv tool install mcp-server-datahub`).
        self._command = command or "mcp-server-datahub"
        self._gms_url = gms_url or os.environ.get(
            "DATAHUB_GMS_URL", "http://localhost:8080"
        )
        self._requests: queue.Queue[
            tuple[str, Mapping[str, Any], Future[Any]] | None
        ] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._startup: Future[None] = Future()

    def _start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="sidq-datahub-mcp", daemon=True
        )
        self._thread.start()
        self._startup.result()

    def _run(self) -> None:
        import anyio

        try:
            anyio.run(self._serve)
        except BaseException as error:  # noqa: BLE001 -- relay failures to the caller
            if not self._startup.done():
                self._startup.set_exception(error)

    async def _serve(self) -> None:
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = _mcp_subprocess_environment(self._gms_url)
        async with (
            stdio_client(StdioServerParameters(command=self._command, env=env)) as (
                read,
                write,
            ),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            self._startup.set_result(None)
            while request := await anyio.to_thread.run_sync(self._requests.get):
                name, arguments, result = request
                try:
                    response = await session.call_tool(name, dict(arguments))
                    result.set_result(_tool_response_payload(response, name=name))
                except BaseException as error:  # noqa: BLE001 -- preserve caller-visible failures
                    result.set_exception(error)

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self._start()
        result: Future[Any] = Future()
        self._requests.put((name, dict(arguments), result))
        return result.result()

    def close(self) -> None:
        if self._thread is not None:
            self._requests.put(None)
            self._thread.join(timeout=5)
        self._thread = None


def _mcp_subprocess_environment(gms_url: str) -> dict[str, str]:
    """Return only the environment needed by the read-only DataHub MCP child."""
    environment = {
        "DATAHUB_GMS_URL": gms_url,
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "HOME": "/tmp",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LOGURU_LEVEL": "WARNING",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    if token := os.environ.get("DATAHUB_GMS_TOKEN"):
        environment["DATAHUB_GMS_TOKEN"] = token
    return environment


def _tool_response_payload(response: Any, *, name: str = "tool") -> Any:
    """Read MCP 1.x camelCase and MCP 2.x snake_case tool results."""
    is_error = getattr(response, "is_error", None)
    if is_error is None:
        is_error = getattr(response, "isError", False)
    if is_error:
        message = " ".join(
            item.text
            for item in response.content
            if getattr(item, "type", "") == "text"
        )
        raise RuntimeError(f"MCP {name} failed: {message}")
    structured = getattr(response, "structured_content", None)
    if structured is None:
        structured = getattr(response, "structuredContent", None)
    if structured is not None:
        return structured
    text = next(
        (item.text for item in response.content if getattr(item, "type", "") == "text"),
        "{}",
    )
    return json.loads(text)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GraphResponseError(f"{name} must be an object")
    return value


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [_mapping(item) for item in value]
    document = _mapping(value)
    if "result" in document:
        return _items(document["result"])
    for key in (
        "results",
        "entities",
        "items",
        "data",
        "schema_fields",
        "fields",
        "paths",
        "searchResults",
    ):
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
        if candidate is not None:
            values = _urns(candidate)
            if values:
                return values
    return []


def _owners(value: Any) -> list[str]:
    """Extract principals only; ownership-type URNs are not owners."""
    document = _mapping(value)
    for key in ("ownership", "owners", "owner_urns", "ownerUrns"):
        candidate = document.get(key)
        if candidate is not None:
            owners = [
                urn
                for urn in _urns(candidate)
                if urn.startswith(("urn:li:corpuser:", "urn:li:corpGroup:"))
            ]
            if owners:
                return owners
    return []


def _markers(value: Any, *keys: str) -> tuple[str, ...]:
    """Column-level tags or terms, unioned across every key that carries them.

    Unlike `_strings` this does not stop at the first key that yields something.
    DataHub reports a field's ingested and hand-edited glossary terms under two
    separate keys, and in the showcase sample the PII marker lives only in the
    second — taking the first would have silently dropped every PII signal.

    It also accepts display names, not just URNs, because `list_schema_fields`
    returns names. Detection works on either; the repair resolves the name to a
    URN before it will write anything.
    """
    document = _mapping(value)
    found: set[str] = set()
    for key in keys:
        candidate = document.get(key)
        if isinstance(candidate, str):
            found.add(candidate)
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str) and item:
                    found.add(item)
                elif isinstance(item, Mapping):
                    found.update(_urns(item))
    return tuple(sorted(found))


def _glossary(value: Any) -> tuple[tuple[str, str], ...]:
    """(display name, URN) for every tag and glossary term named on this asset.

    Column markers arrive from `list_schema_fields` as bare names, and both write
    tools need URNs. The catalog names both forms at asset level, so this is the
    catalog resolving its own vocabulary rather than Sidq inventing a mapping. Tags
    and terms share the map because a field carries either and the URN prefix is
    what later decides which tool writes it back.
    """
    document = _mapping(value)
    pairs: set[tuple[str, str]] = set()
    for container, items_key, item_key in (
        ("glossaryTerms", "terms", "term"),
        ("tags", "tags", "tag"),
    ):
        items = _mapping(document.get(container)).get(items_key)
        for item in items if isinstance(items, list) else ():
            node = _mapping(_mapping(item).get(item_key))
            urn = node.get("urn")
            if not isinstance(urn, str):
                continue
            name = _mapping(node.get("properties")).get("name")
            if isinstance(name, str) and name:
                pairs.add((name, urn))
            # The trailing URN segment is the name DataHub ingests a column marker
            # under; the display name can be decorated ("💲 Large Table") and then
            # matches nothing. Both are recorded so either spelling resolves.
            tail = urn.rsplit(".", 1)[-1] if "." in urn else urn.rsplit(":", 1)[-1]
            if tail:
                pairs.add((tail, urn))
    return tuple(sorted(pairs))


def _urns(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("urn:") else []
    if isinstance(value, list):
        return [urn for item in value for urn in _urns(item)]
    if not isinstance(value, Mapping):
        return []
    found: list[str] = []
    urn = value.get("urn")
    if isinstance(urn, str) and urn.startswith("urn:"):
        found.append(urn)
    for key, item in value.items():
        if key != "urn":
            found.extend(_urns(item))
    return found


def _first_entity(value: Any, urn: str) -> Mapping[str, Any] | None:
    document = _mapping(value)
    if "result" in document:
        return _first_entity(document["result"], urn)
    if (
        urn in document
        and isinstance(document[urn], Mapping)
        and "error" not in document[urn]
    ):
        return _mapping(document[urn])
    if (
        "error" not in document
        and _string(document, "urn", "entity_urn", "entityUrn") == urn
    ):
        return document
    for entity in _items(value):
        if (
            "error" not in entity
            and _string(entity, "urn", "entity_urn", "entityUrn") == urn
        ):
            return entity
    return None


def _parse_fields(value: Any) -> list[SchemaField]:
    fields: list[SchemaField] = []
    for field in _items(value):
        path = _string(field, "path", "field_path", "fieldPath", "name")
        if not path:
            continue
        native_type = (
            _string(
                field,
                "native_type",
                "nativeType",
                "nativeDataType",
                "type",
                "data_type",
                "dataType",
            )
            or ""
        )
        nullable_value = field.get(
            "nullable", field.get("is_nullable", field.get("isNullable", True))
        )
        nullable = (
            nullable_value
            if isinstance(nullable_value, bool)
            else str(nullable_value).lower() in {"true", "yes", "y", "1"}
        )
        fields.append(
            SchemaField(
                path,
                native_type,
                nullable,
                _string(field, "description", "fieldDescription") or None,
                _markers(field, "tags", "globalTags", "editedTags", "tag_urns"),
                _markers(
                    field,
                    "glossaryTerms",
                    "editedGlossaryTerms",
                    "terms",
                    "term_urns",
                ),
            )
        )
    return sorted(fields, key=lambda field: field.path)


def _deprecated(value: Mapping[str, Any]) -> bool:
    candidate = value.get("deprecated", value.get("deprecation"))
    if isinstance(candidate, Mapping):
        candidate = candidate.get("deprecated", candidate.get("is_deprecated", True))
    return candidate is True or str(candidate).lower() in {"true", "yes", "deprecated"}


def _parse_lineage(value: Any, *, requested_column: str | None) -> LineageResult:
    document = _required_mapping(value, "get_lineage response")
    downstreams = document.get("downstreams")
    if not isinstance(downstreams, Mapping):
        raise GraphResponseError("get_lineage response is missing a downstreams object")
    official_empty = _is_official_empty_lineage(downstreams)
    raw_search_results = downstreams.get("searchResults")
    if official_empty:
        search_results: list[Any] = []
    elif isinstance(raw_search_results, list):
        search_results = raw_search_results
    else:
        raise GraphResponseError(
            "get_lineage response is missing a downstreams.searchResults list"
        )
    items = _items(search_results)
    urns: list[str] = []
    entity_types: dict[str, str] = {}
    tags: dict[str, tuple[str, ...]] = {}
    for item in items:
        entity = _mapping(item.get("entity"))
        urn = _string(entity, "urn", "entity_urn", "entityUrn") or _string(
            item, "urn", "entity_urn", "entityUrn", "destination_urn", "destinationUrn"
        )
        if urn:
            urns.append(urn)
            entity_type = _string(
                entity, "type", "entity_type", "entityType"
            ) or _string(item, "entity_type", "entityType", "type")
            if entity_type:
                entity_types[urn] = entity_type
            tags[urn] = tuple(
                sorted(set(_strings(entity, "tags", "tag_urns", "tagUrns")))
            )
    columns = {
        urn: tuple(
            sorted(
                str(column)
                for column in item.get("lineageColumns", ())
                if isinstance(column, str)
            )
        )
        for item in items
        if (urn := _string(_mapping(item.get("entity")), "urn"))
    }
    metadata = _mapping(document.get("metadata"))
    total = downstreams.get("total")
    returned = 0 if official_empty else downstreams.get("returned")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    returned = (
        returned
        if isinstance(returned, int) and not isinstance(returned, bool)
        else None
    )
    has_more = (
        False
        if official_empty
        else downstreams.get("hasMore", downstreams.get("has_more"))
    )
    complete = (
        total is not None
        and returned is not None
        and has_more is False
        and total == returned == len(search_results)
        and len(urns) == len(search_results)
        and len(set(urns)) == len(urns)
    )
    granularity = (
        "column" if metadata.get("queryType") == "column-level-lineage" else "table"
    )
    return LineageResult(
        urns=tuple(sorted(set(urns))),
        entity_types=entity_types,
        paths=tuple(_parse_paths(document.get("paths", []))),
        columns=columns,
        tags=tags,
        granularity=granularity if granularity in {"column", "table"} else "table",
        total=total,
        returned=returned,
        complete=complete,
    )


def _is_official_empty_lineage(value: Mapping[str, Any]) -> bool:
    total = value.get("total")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and total == 0
        and all(
            key not in value
            for key in ("searchResults", "returned", "hasMore", "has_more")
        )
    )


def _parse_paths(
    value: Any, *, default_granularity: str = "table"
) -> list[LineagePath]:
    paths: list[LineagePath] = []
    for item in _items(value):
        raw_nodes = item.get("urns", item.get("path", item.get("nodes", ())))
        urns = (
            tuple(
                node if isinstance(node, str) else _path_node_urn(_mapping(node))
                for node in raw_nodes
            )
            if isinstance(raw_nodes, list)
            else ()
        )
        urns = tuple(node for node in urns if node)
        if urns:
            granularity = str(item.get("granularity", default_granularity))
            paths.append(
                LineagePath(
                    urns, granularity if granularity in {"column", "table"} else "table"
                )
            )
    return sorted(paths, key=lambda path: (path.urns, path.granularity))


def _path_node_urn(node: Mapping[str, Any]) -> str:
    """Normalize both compact and typed MCP lineage-path nodes."""
    urn = _string(node, "urn", "entity_urn", "entityUrn")
    if urn:
        return urn
    parent = _string(_mapping(node.get("parent")), "urn", "entity_urn", "entityUrn")
    field_path = _string(node, "fieldPath", "field_path")
    return _schema_field_urn(parent, field_path) if parent and field_path else ""


def _schema_field_urn(dataset_urn: str, column: str | None) -> str:
    return (
        f"urn:li:schemaField:({dataset_urn},{column})"
        if column is not None
        else dataset_urn
    )


def _downstream_paths(
    paths: Sequence[LineagePath],
    source: str,
    target: str,
    *,
    source_column: str | None = None,
    target_column: str | None = None,
) -> list[LineagePath]:
    """Return only ordered paths whose endpoints prove the downstream request."""
    if not paths:
        return []
    source_node = _schema_field_urn(source, source_column)
    target_node = _schema_field_urn(target, target_column)
    trusted = [
        path
        for path in paths
        if path.urns and path.urns[0] == source_node and path.urns[-1] == target_node
    ]
    if trusted:
        return trusted
    return []
