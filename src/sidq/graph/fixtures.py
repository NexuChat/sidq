"""Record and replay the narrow graph contract without a running DataHub stack."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sidq.graph.client import (
    DatasetInfo,
    GraphClient,
    LineagePath,
    LineageResult,
    _downstream_paths,
)
from sidq.graph.client import Path as GraphPath
from sidq.serialization import canonical_data


class GraphFixtureError(RuntimeError):
    """A requested response was intentionally not recorded in the fixture set."""


def _key(method: str, *args: Any, **kwargs: Any) -> str:
    payload = json.dumps(
        canonical_data({"args": args, "kwargs": kwargs}),
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{method}-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def _encode(value: Any) -> Any:
    return canonical_data(value)


class RecordingGraphClient:
    """Persist responses from another graph client as individually inspectable JSON."""

    def __init__(self, client: GraphClient, fixture_dir: str | Path) -> None:
        self._client = client
        self._fixture_dir = Path(fixture_dir)
        self._fixture_dir.mkdir(parents=True, exist_ok=True)

    def _record(self, method: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        name = _key(method, *args, **kwargs)
        (self._fixture_dir / f"{name}.json").write_text(
            json.dumps(_encode(value), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_path = self._fixture_dir / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )
        manifest[name] = f"{name}.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return value

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self._record("get_dataset", self._client.get_dataset(urn), urn)

    def find_dataset(self, name_or_urn: str) -> str | None:
        return self._record(
            "find_dataset", self._client.find_dataset(name_or_urn), name_or_urn
        )

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        return self._record(
            "get_downstream",
            self._client.get_downstream(urn, depth, column),
            urn,
            depth,
            column=column,
        )

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> list[GraphPath]:
        return self._record(
            "paths_between",
            self._client.paths_between(a, b, source_column, target_column),
            a,
            b,
            source_column=source_column,
            target_column=target_column,
        )


class ReplayGraphClient:
    """Read only recorded graph responses; missing data is an explicit failure."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self._fixture_dir = Path(fixture_dir)
        manifest_path = self._fixture_dir / "manifest.json"
        self._manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )

    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        name = _key(method, *args, **kwargs)
        filename = self._manifest.get(name, f"{name}.json")
        path = self._fixture_dir / filename
        if not path.exists():
            raise GraphFixtureError(
                f"no replay fixture for {method}({args!r}, {kwargs!r})"
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        raw = self._read("get_dataset", urn)
        return _dataset(raw) if raw is not None else None

    def get_documentation(self, urn: str) -> dict[str, Any] | None:
        """Serve the descriptions the recorded snapshot already carries.

        `DocRotGate` reads documentation through this optional method, and until
        now nothing but a test stub implemented it — so the doc-rot check returned
        empty against both the replay snapshot and the live client, and produced
        zero evidence across 20,666 labelled runs while the README advertised it as
        a truth check. The recordings hold `field_descriptions` already; this hands
        them over rather than re-fetching or re-deriving anything.
        """
        raw = self._read("get_dataset", urn)
        return raw if isinstance(raw, dict) else None

    def find_dataset(self, name_or_urn: str) -> str | None:
        raw = self._read("find_dataset", name_or_urn)
        return raw if isinstance(raw, str) else None

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        return _lineage(self._read("get_downstream", urn, depth, column=column))

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> list[GraphPath]:
        try:
            raw = self._read(
                "paths_between",
                a,
                b,
                source_column=source_column,
                target_column=target_column,
            )
        except GraphFixtureError:
            if source_column is not None or target_column is not None:
                raise
            # Compatibility with recordings made before column path parameters
            # were added to the narrow graph contract.
            raw = self._read("paths_between", a, b)
        return _downstream_paths(
            [_path(item) for item in raw],
            a,
            b,
            source_column=source_column,
            target_column=target_column,
        )


def _dataset(raw: dict[str, Any]) -> DatasetInfo:
    from sidq.graph.client import SchemaField

    return DatasetInfo(
        urn=raw["urn"],
        fields=tuple(SchemaField(**item) for item in raw.get("fields", ())),
        tags=tuple(raw.get("tags", ())),
        terms=tuple(raw.get("terms", ())),
        owners=tuple(raw.get("owners", ())),
        deprecated=bool(raw.get("deprecated", False)),
    )


def _path(raw: dict[str, Any]) -> LineagePath:
    note = raw.get("note")
    return LineagePath(
        tuple(raw.get("urns", ())),
        str(raw.get("granularity", "table")),
        note if isinstance(note, str) else None,
    )


def _lineage(raw: dict[str, Any]) -> LineageResult:
    entity_types = raw.get("entity_types", {})
    urns = tuple(raw.get("urns", ()))
    total = raw.get("total")
    returned = raw.get("returned")
    total = total if isinstance(total, int) and not isinstance(total, bool) else None
    returned = (
        returned
        if isinstance(returned, int) and not isinstance(returned, bool)
        else None
    )
    explicit_complete = raw.get("complete")
    if total is not None or returned is not None:
        # A legacy bounded recording that omitted the final completeness bit is
        # not allowed to infer it from partial counts. Only the old unbounded
        # LineageResult shape (which carried neither count) keeps its historical
        # behaviour, so the committed pre-contract fixture corpus still replays.
        complete = (
            explicit_complete is not False
            and total is not None
            and total == returned == len(urns)
        )
    elif isinstance(explicit_complete, bool):
        complete = explicit_complete
    else:
        complete = True
    return LineageResult(
        urns=urns,
        entity_types=entity_types
        if isinstance(entity_types, dict)
        else tuple(entity_types),
        paths=tuple(_path(item) for item in raw.get("paths", ())),
        columns={key: tuple(value) for key, value in raw.get("columns", {}).items()},
        tags={key: tuple(value) for key, value in raw.get("tags", {}).items()},
        granularity=str(raw.get("granularity", "table")),
        total=total,
        returned=returned,
        complete=complete,
    )
