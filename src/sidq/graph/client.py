"""A transport-independent view of the small slice of DataHub gates need."""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path as FilePath
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
    columns: Mapping[str, tuple[str, ...]] = ()
    tags: Mapping[str, tuple[str, ...]] = ()
    granularity: str = "table"


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
        metadata = _mapping(_mapping(raw).get("metadata"))
        default_granularity = (
            "column" if metadata.get("pathType") == "column-level" else "table"
        )
        return list(_parse_paths(raw, default_granularity=default_granularity))

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
        self._command = command or str(
            FilePath(__file__).resolve().parents[3]
            / ".venv"
            / "bin"
            / "mcp-server-datahub"
        )
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

        env = {
            **os.environ,
            "DATAHUB_GMS_URL": self._gms_url,
            "DATAHUB_TELEMETRY_ENABLED": "false",
            "LOGURU_LEVEL": "WARNING",
        }
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
                    if response.isError:
                        text = " ".join(
                            item.text
                            for item in response.content
                            if getattr(item, "type", "") == "text"
                        )
                        raise RuntimeError(f"MCP {name} failed: {text}")
                    if response.structuredContent is not None:
                        result.set_result(response.structuredContent)
                    else:
                        text = next(
                            (
                                item.text
                                for item in response.content
                                if getattr(item, "type", "") == "text"
                            ),
                            "{}",
                        )
                        result.set_result(json.loads(text))
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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


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
        if "error" not in entity and _string(
            entity, "urn", "entity_urn", "entityUrn"
        ) in (None, urn):
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
        fields.append(SchemaField(path, native_type, nullable))
    return sorted(fields, key=lambda field: field.path)


def _deprecated(value: Mapping[str, Any]) -> bool:
    candidate = value.get("deprecated", value.get("deprecation"))
    if isinstance(candidate, Mapping):
        candidate = candidate.get("deprecated", candidate.get("is_deprecated", True))
    return candidate is True or str(candidate).lower() in {"true", "yes", "deprecated"}


def _parse_lineage(value: Any, *, requested_column: str | None) -> LineageResult:
    document = _mapping(value)
    downstreams = _mapping(document.get("downstreams"))
    items = _items(downstreams.get("searchResults", ()))
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
    )


def _parse_paths(
    value: Any, *, default_granularity: str = "table"
) -> list[LineagePath]:
    paths: list[LineagePath] = []
    for item in _items(value):
        raw_nodes = item.get("urns", item.get("path", item.get("nodes", ())))
        urns = (
            tuple(
                node
                if isinstance(node, str)
                else _string(node, "urn", "entity_urn", "entityUrn") or ""
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
