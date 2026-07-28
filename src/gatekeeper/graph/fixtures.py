"""Record and replay the narrow graph contract without a running DataHub stack."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from sidq.graph.client import DatasetInfo, GraphClient, LineagePath, LineageResult
from sidq.graph.client import Path as GraphPath
from sidq.serialization import canonical_data


class GraphFixtureError(RuntimeError):
    """A requested response was intentionally not recorded in the fixture set."""


def _key(method: str, *args: Any, **kwargs: Any) -> str:
    payload = json.dumps(canonical_data({"args": args, "kwargs": kwargs}), sort_keys=True, separators=(",", ":"))
    return f"{method}-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def _encode(value: Any) -> Any:
    return canonical_data(asdict(value) if is_dataclass(value) else value)


class RecordingGraphClient:
    """Persist responses from another graph client as individually inspectable JSON."""

    def __init__(self, client: GraphClient, fixture_dir: str | Path) -> None:
        self._client = client
        self._fixture_dir = Path(fixture_dir)
        self._fixture_dir.mkdir(parents=True, exist_ok=True)

    def _record(self, method: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        name = _key(method, *args, **kwargs)
        (self._fixture_dir / f"{name}.json").write_text(
            json.dumps(_encode(value), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_path = self._fixture_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        manifest[name] = f"{name}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return value

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        return self._record("get_dataset", self._client.get_dataset(urn), urn)

    def find_dataset(self, name_or_urn: str) -> str | None:
        return self._record("find_dataset", self._client.find_dataset(name_or_urn), name_or_urn)

    def get_downstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult:
        return self._record("get_downstream", self._client.get_downstream(urn, depth, column), urn, depth, column=column)

    def paths_between(self, a: str, b: str) -> list[GraphPath]:
        return self._record("paths_between", self._client.paths_between(a, b), a, b)


class ReplayGraphClient:
    """Read only recorded graph responses; missing data is an explicit failure."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self._fixture_dir = Path(fixture_dir)
        manifest_path = self._fixture_dir / "manifest.json"
        self._manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    def _read(self, method: str, *args: Any, **kwargs: Any) -> Any:
        name = _key(method, *args, **kwargs)
        filename = self._manifest.get(name, f"{name}.json")
        path = self._fixture_dir / filename
        if not path.exists():
            raise GraphFixtureError(f"no replay fixture for {method}({args!r}, {kwargs!r})")
        return json.loads(path.read_text(encoding="utf-8"))

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        raw = self._read("get_dataset", urn)
        return _dataset(raw) if raw is not None else None

    def find_dataset(self, name_or_urn: str) -> str | None:
        raw = self._read("find_dataset", name_or_urn)
        return raw if isinstance(raw, str) else None

    def get_downstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult:
        return _lineage(self._read("get_downstream", urn, depth, column=column))

    def paths_between(self, a: str, b: str) -> list[GraphPath]:
        raw = self._read("paths_between", a, b)
        return [_path(item) for item in raw]


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
    return LineagePath(tuple(raw.get("urns", ())), str(raw.get("granularity", "table")))


def _lineage(raw: dict[str, Any]) -> LineageResult:
    entity_types = raw.get("entity_types", {})
    return LineageResult(
        urns=tuple(raw.get("urns", ())),
        entity_types=entity_types if isinstance(entity_types, dict) else tuple(entity_types),
        paths=tuple(_path(item) for item in raw.get("paths", ())),
        granularity=str(raw.get("granularity", "table")),
    )
