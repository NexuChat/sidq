#!/usr/bin/env python3
"""Build deterministic graph-replay fixtures for the bundled dbt demo.

The replay client keys fixture files from the canonical method arguments.  This
script deliberately imports that key implementation and resolves every model
with :class:`sidq.resolver.Resolver`, rather than duplicating either convention.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from sidq.graph.fixtures import _key
from sidq.resolver import Resolver

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = ROOT / "demo" / "dbt"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "graph"
PII_TAG = "urn:li:tag:PII"
TEAM_BY_LAYER = {
    "staging": "urn:li:corpgroup:commerce-data-platform",
    "intermediate": "urn:li:corpgroup:commerce-analytics",
    "marts": "urn:li:corpgroup:analytics-engineering",
    "legacy": "urn:li:corpgroup:order-entry",
    "support": "urn:li:corpgroup:source-systems",
    "bi": "urn:li:corpgroup:business-intelligence",
}

CUSTOMER_360_CHART = "urn:li:chart:(looker,sidq_demo.customer_360_lifecycle)"
REVENUE_DAILY_CHART = "urn:li:chart:(looker,sidq_demo.revenue_daily_trend)"
EXECUTIVE_DASHBOARD = "urn:li:dashboard:(looker,sidq_demo.executive_metrics)"


@dataclass(frozen=True, slots=True)
class Model:
    path: str
    name: str
    urn: str
    layer: str
    fields: tuple[str, ...]
    descriptions: Mapping[str, str]
    pii_fields: frozenset[str]
    native_types: Mapping[str, str]
    dependency_names: tuple[str, ...]


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _schema_columns(root: Path) -> dict[str, tuple[dict[str, object], ...]]:
    models: dict[str, tuple[dict[str, object], ...]] = {}
    for path in sorted(root.glob("models/**/schema.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for model in document.get("models", ()):
            if not isinstance(model, Mapping) or not isinstance(model.get("name"), str):
                continue
            columns = model.get("columns", ())
            if isinstance(columns, list):
                models[model["name"]] = tuple(
                    dict(column) for column in columns if isinstance(column, Mapping)
                )
    return models


def _native_types(sql: str) -> dict[str, str]:
    """Infer only explicit SQL casts; uncast fields remain the safe TEXT default."""
    types: dict[str, str] = {}
    pattern = re.compile(
        r"::\s*([a-zA-Z]+(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?)\s+as\s+([a-zA-Z_][\w]*)",
        re.IGNORECASE,
    )
    for type_name, field in pattern.findall(sql):
        types[field] = re.sub(r"\s+", " ", type_name).lower()
    return types


def _load_models(root: Path) -> tuple[Model, ...]:
    resolver = Resolver(root)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    nodes = manifest.get("nodes", {})
    nodes_by_path = {
        str(node.get("original_file_path")): node
        for node in nodes.values()
        if isinstance(node, Mapping) and isinstance(node.get("original_file_path"), str)
    }
    schema = _schema_columns(root)
    models: list[Model] = []
    for sql_path in sorted(root.glob("models/**/*.sql")):
        relative = sql_path.relative_to(root).as_posix()
        resolved = resolver.resolve([relative]).touched_assets
        # customer_revenue.sql is intentionally not in dbt's manifest.  It is not
        # one of the 19 resolved demo models this fixture graph can represent.
        if len(resolved) != 1:
            continue
        asset = resolved[0]
        node = nodes_by_path.get(relative, {})
        name = str(node.get("name") or sql_path.stem)
        column_specs = schema.get(name, ())
        if column_specs:
            fields = tuple(str(column["name"]) for column in column_specs)
            descriptions = {
                str(column["name"]): str(column.get("description", ""))
                for column in column_specs
            }
            pii_fields = frozenset(
                str(column["name"])
                for column in column_specs
                if isinstance(column.get("meta"), Mapping)
                and column["meta"].get("pii") is True
            )
        else:
            fields = tuple(sorted({reference.field_path for reference in asset.referenced_fields}))
            descriptions = {}
            pii_fields = frozenset()
        dependencies = tuple(
            str(item).rsplit(".", 1)[-1]
            for item in node.get("depends_on", {}).get("nodes", ())
            if isinstance(item, str) and item.startswith("model.")
        ) if isinstance(node, Mapping) else ()
        parent = Path(relative).parent.name
        layer = parent if parent in {"staging", "intermediate", "marts"} else "legacy"
        models.append(
            Model(
                path=relative,
                name=name,
                urn=asset.urn,
                layer=layer,
                fields=fields,
                descriptions=descriptions,
                pii_fields=pii_fields,
                native_types=_native_types(sql_path.read_text(encoding="utf-8")),
                dependency_names=dependencies,
            )
        )
    return tuple(sorted(models, key=lambda model: model.path))


def _mutation_fields() -> dict[str, set[str]]:
    """Include generated-corpus aliases so the benchmark remains a replay, not a miss."""
    path = ROOT / "data" / "benchmark" / "mutations.jsonl"
    fields: dict[str, set[str]] = defaultdict(set)
    if not path.is_file():
        return fields
    try:
        import sqlglot
    except ImportError:
        return fields
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        model_path = record.get("model_path")
        sql = record.get("mutated_sql")
        if not isinstance(model_path, str) or not isinstance(sql, str):
            continue
        try:
            select = sqlglot.parse_one(sql).find(sqlglot.exp.Select)
        except Exception:  # Corpus entries must never make fixture generation fail.
            continue
        if select is not None:
            fields[model_path].update(
                projection.alias_or_name
                for projection in select.expressions
                if projection.alias_or_name and projection.alias_or_name != "*"
            )
    return fields


def _dataset_payload(
    urn: str,
    fields: Iterable[str],
    *,
    native_types: Mapping[str, str] = {},
    descriptions: Mapping[str, str] = {},
    pii_fields: Iterable[str] = (),
    owner: str,
    tags: Iterable[str] = (),
) -> dict[str, object]:
    pii = set(pii_fields)
    all_tags = sorted({*tags, *(PII_TAG for field in pii if field)})
    names = tuple(sorted(set(fields)))
    return {
        "deprecated": False,
        "field_descriptions": {field: descriptions[field] for field in names if descriptions.get(field)},
        "fields": [
            {"native_type": native_types.get(field, "text"), "nullable": True, "path": field}
            for field in names
        ],
        "owners": [owner],
        "tags": all_tags,
        "terms": [],
        "urn": urn,
    }


def _write_fixture(
    fixture_dir: Path,
    manifest: dict[str, str],
    method: str,
    payload: object,
    *args: object,
    overwrite: bool = False,
    **kwargs: object,
) -> tuple[str, bool]:
    name = _key(method, *args, **kwargs)
    filename = f"{name}.json"
    path = fixture_dir / filename
    encoded = _json(payload)
    changed = False
    if overwrite or not path.exists():
        if not path.exists() or path.read_text(encoding="utf-8") != encoded:
            path.write_text(encoded, encoding="utf-8")
            changed = True
    if manifest.get(name) != filename:
        manifest[name] = filename
        changed = True
    return filename, changed


def _descendants(models: Mapping[str, Model]) -> dict[str, tuple[str, ...]]:
    children: dict[str, set[str]] = defaultdict(set)
    for model in models.values():
        for dependency in model.dependency_names:
            if dependency in models:
                children[dependency].add(model.name)
    result: dict[str, tuple[str, ...]] = {}
    for name in models:
        discovered: set[str] = set()
        queue = deque(children[name])
        while queue:
            child = queue.popleft()
            if child in discovered:
                continue
            discovered.add(child)
            queue.extend(children[child])
        result[name] = tuple(sorted(discovered))
    return result


def _schema_field_urn(urn: str, field: str) -> str:
    return f"urn:li:schemaField:({urn},{field})"


def _path_payload(urns: Sequence[str], granularity: str) -> list[dict[str, object]]:
    return [{"granularity": granularity, "urns": list(urns)}]


def build() -> tuple[dict[str, list[str]], int]:
    fixture_dir = FIXTURE_DIR
    fixture_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = fixture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    models = _load_models(DEMO_ROOT)
    expected = 19
    if len(models) != expected:
        raise RuntimeError(f"expected {expected} resolved demo models, found {len(models)}")
    by_name = {model.name: model for model in models}
    descendants = _descendants(by_name)
    mutation_fields = _mutation_fields()
    written: dict[str, list[str]] = defaultdict(list)
    changes = 0

    existing_dataset_urns = set()
    for path in fixture_dir.glob("get_dataset-*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping) and isinstance(payload.get("urn"), str):
            existing_dataset_urns.add(payload["urn"])

    # Canonical model fixtures, preserving the byte-stable legacy baseline.
    for model in models:
        if model.urn not in existing_dataset_urns:
            filename, changed = _write_fixture(
                fixture_dir,
                manifest,
                "get_dataset",
                _dataset_payload(
                    model.urn,
                    model.fields,
                    native_types=model.native_types,
                    descriptions=model.descriptions,
                    pii_fields=model.pii_fields,
                    owner=TEAM_BY_LAYER[model.layer],
                ),
                model.urn,
            )
            written[model.urn].append(filename)
            changes += int(changed)

    # Replay exactly the reference URNs that Resolver emits, including abbreviated
    # relation names and CTE-derived references.  These are support entities, not
    # alternate identities for a dbt model.
    references: dict[str, set[str]] = defaultdict(set)
    resolver = Resolver(DEMO_ROOT)
    for model in models:
        asset = resolver.resolve([model.path]).touched_assets[0]
        for reference in asset.referenced_fields:
            references[reference.dataset_urn].add(reference.field_path)
    for urn, fields in sorted(references.items()):
        if urn in existing_dataset_urns or urn in {model.urn for model in models}:
            continue
        filename, changed = _write_fixture(
            fixture_dir,
            manifest,
            "get_dataset",
            _dataset_payload(
                urn,
                fields,
                pii_fields=(field for field in fields if any(token in field.lower() for token in ("email", "phone", "address", "ip_"))),
                owner=TEAM_BY_LAYER["support"],
            ),
            urn,
        )
        written[urn].append(filename)
        changes += int(changed)

    bi_entities = {
        CUSTOMER_360_CHART: ("CHART", (PII_TAG, "urn:li:tag:critical")),
        REVENUE_DAILY_CHART: ("CHART", ()),
        EXECUTIVE_DASHBOARD: ("DASHBOARD", ("urn:li:tag:critical",)),
    }
    for urn, (_, tags) in sorted(bi_entities.items()):
        filename, changed = _write_fixture(
            fixture_dir,
            manifest,
            "get_dataset",
            _dataset_payload(
                urn,
                (),
                owner=TEAM_BY_LAYER["bi"],
                tags=tags,
            ),
            urn,
            overwrite=True,
        )
        written[urn].append(filename)
        changes += int(changed)

    chart_for_mart = {
        "customer_360": CUSTOMER_360_CHART,
        "revenue_daily": REVENUE_DAILY_CHART,
    }
    for model in models:
        downstream_models = [by_name[name] for name in descendants[model.name]]
        urns = [child.urn for child in downstream_models]
        if model.name in chart_for_mart:
            urns.extend((chart_for_mart[model.name], EXECUTIVE_DASHBOARD))
        elif any(child.name in chart_for_mart for child in downstream_models):
            urns.append(EXECUTIVE_DASHBOARD)
        entity_types = {child.urn: "DATASET" for child in downstream_models}
        if model.name in chart_for_mart:
            entity_types[chart_for_mart[model.name]] = "CHART"
            entity_types[EXECUTIVE_DASHBOARD] = "DASHBOARD"
        elif EXECUTIVE_DASHBOARD in urns:
            entity_types[EXECUTIVE_DASHBOARD] = "DASHBOARD"
        table_payload = {
            "columns": {urn: [] for urn in sorted(set(urns))},
            "entity_types": dict(sorted(entity_types.items())),
            "granularity": "table",
            "metadata": {"queryType": "table-lineage"},
            "paths": [],
            "tags": {urn: [] for urn in sorted(set(urns))},
            "urns": sorted(set(urns)),
        }
        filename, changed = _write_fixture(
            fixture_dir, manifest, "get_downstream", table_payload, model.urn, 3, column=None
        )
        written[model.urn].append(filename)
        changes += int(changed)

        fields = set(model.fields) | mutation_fields.get(model.path, set())
        for field in sorted(fields):
            column_targets: dict[str, str] = {}
            for child in downstream_models:
                if field in child.fields:
                    column_targets[child.urn] = field
            if model.name in chart_for_mart and field in model.pii_fields:
                column_targets[chart_for_mart[model.name]] = field
                column_targets[EXECUTIVE_DASHBOARD] = field
            target_urns = sorted(column_targets)
            column_payload = {
                "columns": {urn: [column_targets[urn]] for urn in target_urns},
                "entity_types": {
                    urn: (
                        "CHART" if urn.startswith("urn:li:chart:") else
                        "DASHBOARD" if urn.startswith("urn:li:dashboard:") else "DATASET"
                    )
                    for urn in target_urns
                },
                "granularity": "column",
                "metadata": {"queryType": "column-level-lineage"},
                "paths": [],
                "tags": {
                    urn: [PII_TAG] if field in model.pii_fields and urn in column_targets else []
                    for urn in target_urns
                },
                "urns": target_urns,
            }
            filename, changed = _write_fixture(
                fixture_dir, manifest, "get_downstream", column_payload, model.urn, 3, column=field
            )
            written[model.urn].append(filename)
            changes += int(changed)

        if EXECUTIVE_DASHBOARD in urns:
            route = [model.urn]
            if model.name in chart_for_mart:
                route.append(chart_for_mart[model.name])
            else:
                next_mart = next(
                    (child for child in downstream_models if child.name in chart_for_mart), None
                )
                if next_mart is not None:
                    route.extend((next_mart.urn, chart_for_mart[next_mart.name]))
            route.append(EXECUTIVE_DASHBOARD)
            filename, changed = _write_fixture(
                fixture_dir, manifest, "paths_between", _path_payload(route, "table"), model.urn, EXECUTIVE_DASHBOARD,
                source_column=None, target_column=None,
            )
            written[model.urn].append(filename)
            changes += int(changed)

    for mart_name, chart_urn in sorted(chart_for_mart.items()):
        mart = by_name[mart_name]
        for field in sorted(mart.pii_fields):
            filename, changed = _write_fixture(
                fixture_dir,
                manifest,
                "paths_between",
                _path_payload(
                    (_schema_field_urn(mart.urn, field), _schema_field_urn(chart_urn, field)),
                    "column",
                ),
                mart.urn,
                chart_urn,
                source_column=field,
                target_column=field,
            )
            written[mart.urn].append(filename)
            changes += int(changed)
        filename, changed = _write_fixture(
            fixture_dir,
            manifest,
            "paths_between",
            _path_payload((chart_urn, EXECUTIVE_DASHBOARD), "table"),
            chart_urn,
            EXECUTIVE_DASHBOARD,
            source_column=None,
            target_column=None,
        )
        written[mart.urn].append(filename)
        changes += int(changed)

    manifest_text = _json(manifest)
    if manifest_path.read_text(encoding="utf-8") != manifest_text:
        manifest_path.write_text(manifest_text, encoding="utf-8")
        changes += 1
    return dict(sorted(written.items())), changes


def main() -> int:
    written, changes = build()
    print("URN | fixture files written")
    print("--- | ---")
    for urn, files in written.items():
        print(f"{urn} | {', '.join(sorted(files))}")
    print(f"changes: {changes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
