"""Pure resolution of changed SQL files to DataHub dataset URNs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from sidq.models import Evidence, FieldRef, TouchedAsset


@dataclass(frozen=True, slots=True)
class NamingConvention:
    """The intentionally small path-to-dataset convention used outside dbt."""

    platform: str
    environment: str = "PROD"
    path_pattern: str = "models/{schema}/{table}.sql"
    relation_template: str = "{schema}.{table}"

    def urn_for_path(self, source_path: str) -> str | None:
        pattern = re.escape(self.path_pattern.replace("\\", "/"))
        pattern = pattern.replace(re.escape("{schema}"), r"(?P<schema>[^/]+)")
        pattern = pattern.replace(re.escape("{table}"), r"(?P<table>[^/.]+)")
        match = re.fullmatch(pattern, source_path.replace("\\", "/"))
        if match is None:
            return None
        try:
            relation = self.relation_template.format(**match.groupdict())
        except KeyError:
            return None
        return dataset_urn(self.platform, relation, self.environment)


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Resolver output: assets plus all evidence produced while resolving."""

    touched_assets: tuple[TouchedAsset, ...]
    evidence: tuple[Evidence, ...]

    @property
    def touched(self) -> tuple[TouchedAsset, ...]:
        return self.touched_assets


def dataset_urn(platform: str, relation_name: str, environment: str = "PROD") -> str:
    """Build a DataHub dataset URN without any external lookup."""
    if relation_name.startswith("urn:li:dataset:"):
        return relation_name
    relation = ".".join(part.strip().strip('"`[]') for part in relation_name.split("."))
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{relation},{environment})"


def _urn_platform_and_environment(urn: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),.+,([^,)]+)\)", urn
    )
    return (match.group(1), match.group(2)) if match else None


def _normalise_path(path: str | Path, root: Path) -> str | None:
    candidate = Path(path)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    )
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _read_yaml_mapping(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, Mapping) else {}


def _assets_config(
    root: Path, assets_path: Path | None
) -> tuple[dict[str, str], NamingConvention | None]:
    path = assets_path or root / ".sidq" / "assets.yml"
    document = _read_yaml_mapping(path)
    source_map = document.get("assets", document.get("path_to_urn", {}))
    if not source_map:
        source_map = {
            key: value for key, value in document.items() if isinstance(value, str)
        }
    explicit: dict[str, str] = {}
    if isinstance(source_map, Mapping):
        for key, value in source_map.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            normalised = _normalise_path(key, root)
            if normalised is not None:
                explicit[normalised] = value
    naming_raw = document.get("naming_convention", document.get("naming"))
    if not isinstance(naming_raw, Mapping) or not isinstance(
        naming_raw.get("platform"), str
    ):
        return explicit, None
    allowed = {"platform", "environment", "path_pattern", "relation_template"}
    try:
        return explicit, NamingConvention(
            **{key: value for key, value in naming_raw.items() if key in allowed}
        )
    except TypeError:
        return explicit, None


def _manifest_candidates(root: Path, manifest_path: Path | None) -> tuple[Path, ...]:
    if manifest_path is not None:
        return (manifest_path,)
    candidates = [root / "target" / "manifest.json", root / "manifest.json"]
    return tuple(candidate for candidate in candidates if candidate.is_file())


def _manifest_index(
    root: Path, manifest_path: Path | None
) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    for candidate in _manifest_candidates(root, manifest_path):
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = document.get("metadata", {}) if isinstance(document, Mapping) else {}
        platform = (
            metadata.get("adapter_type", "dbt")
            if isinstance(metadata, Mapping)
            else "dbt"
        )
        nodes = document.get("nodes", {}) if isinstance(document, Mapping) else {}
        if not isinstance(nodes, Mapping):
            continue
        indexed: dict[str, tuple[str, str, tuple[str, ...]]] = {}
        for node in nodes.values():
            if not isinstance(node, Mapping):
                continue
            relation = node.get("relation_name")
            source_path = node.get("original_file_path", node.get("path"))
            if not isinstance(relation, str) or not isinstance(source_path, str):
                continue
            config = node.get("config", {})
            config = config if isinstance(config, Mapping) else {}
            config_meta = config.get("meta", {})
            config_meta = config_meta if isinstance(config_meta, Mapping) else {}
            env = config_meta.get("environment", "PROD")
            if not isinstance(env, str):
                env = "PROD"
            node_meta = node.get("meta", {})
            node_meta = node_meta if isinstance(node_meta, Mapping) else {}
            urn = node_meta.get("dataset_urn")
            if not isinstance(urn, str):
                urn = dataset_urn(str(platform), relation, env)
            columns = node.get("columns", {})
            previous_fields = (
                tuple(sorted(str(name) for name in columns))
                if isinstance(columns, Mapping)
                else ()
            )
            normalised = _normalise_path(source_path, root)
            if normalised is not None:
                indexed[normalised] = (urn, "dbt_manifest", previous_fields)
        if indexed:
            return indexed
    return {}


def _sql_parts(
    sql: str, default_urn: str, convention: NamingConvention | None
) -> tuple[tuple[str, ...], tuple[FieldRef, ...]]:
    """Extract output aliases and referenced table columns with sqlglot."""
    import sqlglot
    from sqlglot import exp

    expression = sqlglot.parse_one(sql)
    aliases = tuple(
        sorted(
            {
                projection.alias_or_name
                for select in expression.find_all(exp.Select)
                for projection in select.expressions
                if projection.alias_or_name and projection.alias_or_name != "*"
            }
        )
    )
    default_platform = _urn_platform_and_environment(default_urn)
    ctes = {
        cte.alias_or_name: cte
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    query_sources: dict[int, dict[str, str | dict[str, str | None]]] = {}
    cte_projections: dict[str, dict[str, str | None]] = {}

    def table_urn(table: Any) -> str:
        relation = ".".join(
            part for part in (table.catalog, table.db, table.name) if part
        )
        if convention is not None:
            return dataset_urn(convention.platform, relation, convention.environment)
        if default_platform is not None:
            return dataset_urn(default_platform[0], relation, default_platform[1])
        return default_urn

    def source_urn(source: str | dict[str, str | None], field: str) -> str | None:
        return source if isinstance(source, str) else source.get(field)

    def projection_urn(
        expression: Any, sources: dict[str, str | dict[str, str | None]]
    ) -> str | None:
        columns = list(expression.find_all(exp.Column))
        urns: set[str] = set()
        for column in columns:
            if not column.name or column.name == "*":
                continue
            if column.table:
                source = sources.get(column.table)
                urn = source_urn(source, column.name) if source is not None else None
                if urn is None:
                    return None
                urns.add(urn)
                continue
            candidates = {
                urn
                for source in sources.values()
                if (urn := source_urn(source, column.name)) is not None
            }
            if len(candidates) != 1:
                return None
            urns.update(candidates)
        return next(iter(urns)) if len(urns) == 1 else None

    def projections(query: Any) -> dict[str, str | None]:
        sources = query_source_map(query)
        return {
            projection.alias_or_name: projection_urn(projection, sources)
            for projection in query.expressions
            if projection.alias_or_name and projection.alias_or_name != "*"
        }

    def cte_projection(name: str) -> dict[str, str | None]:
        if name not in cte_projections:
            cte_projections[name] = projections(ctes[name].this)
        return cte_projections[name]

    def query_source_map(query: Any) -> dict[str, str | dict[str, str | None]]:
        cached = query_sources.get(id(query))
        if cached is not None:
            return cached
        sources: dict[str, str | dict[str, str | None]] = {}
        query_sources[id(query)] = sources
        from_ = query.args.get("from_")
        joins = query.args.get("joins") or []
        relations = [from_.this] if from_ is not None and from_.this is not None else []
        relations.extend(join.this for join in joins if join.this is not None)
        for relation_node in relations:
            if isinstance(relation_node, exp.Table):
                name = relation_node.name
                if not name:
                    continue
                source = (
                    cte_projection(name) if name in ctes else table_urn(relation_node)
                )
                sources[name] = source
                if relation_node.alias:
                    sources[relation_node.alias] = source
            elif isinstance(relation_node, exp.Subquery) and relation_node.alias:
                sources[relation_node.alias] = projections(relation_node.this)
        return sources

    for select in expression.find_all(exp.Select):
        query_source_map(select)

    references: set[FieldRef] = set()
    for column in expression.find_all(exp.Column):
        if not column.name or column.name == "*":
            continue
        urn = default_urn
        if column.table:
            parent = column.parent
            while parent is not None and not isinstance(parent, exp.Select):
                parent = parent.parent
            sources = query_sources.get(id(parent), {}) if parent is not None else {}
            source = sources.get(column.table)
            if source is not None:
                resolved_urn = source_urn(source, column.name)
                if resolved_urn is None:
                    raise ValueError(
                        f"could not resolve {column.table}.{column.name} through a derived relation"
                    )
                urn = resolved_urn
        references.add(FieldRef(urn, column.name))
    return aliases, tuple(
        sorted(references, key=lambda ref: (ref.dataset_urn, ref.field_path))
    )


class Resolver:
    def __init__(
        self,
        repo_root: str | Path,
        *,
        manifest_path: str | Path | None = None,
        assets_path: str | Path | None = None,
        naming_convention: NamingConvention | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.manifest_path = (
            (
                self.repo_root / manifest_path
                if not Path(manifest_path).is_absolute()
                else Path(manifest_path)
            )
            if manifest_path is not None
            else None
        )
        self.assets_path = (
            (
                self.repo_root / assets_path
                if not Path(assets_path).is_absolute()
                else Path(assets_path)
            )
            if assets_path is not None
            else None
        )
        self.naming_convention = naming_convention

    def resolve(self, changed_files: Sequence[str | Path]) -> ResolutionResult:
        manifest = _manifest_index(self.repo_root, self.manifest_path)
        explicit, configured_convention = _assets_config(
            self.repo_root, self.assets_path
        )
        convention = self.naming_convention or configured_convention
        assets: list[TouchedAsset] = []
        evidence: list[Evidence] = []
        normalised_files: set[str] = set()
        for item in changed_files:
            changed_file = _normalise_path(item, self.repo_root)
            if changed_file is None:
                subject = Path(item).name
                evidence.append(
                    Evidence("unresolved_asset", subject, {"source_path": subject})
                )
            else:
                normalised_files.add(changed_file)
        for changed_file in sorted(normalised_files):
            resolved = manifest.get(changed_file)
            if resolved is None and changed_file in explicit:
                resolved = (explicit[changed_file], "explicit_map", ())
            if resolved is None and convention is not None:
                urn = convention.urn_for_path(changed_file)
                if urn is not None:
                    resolved = (urn, "naming_convention", ())
            if resolved is None:
                evidence.append(
                    Evidence(
                        "unresolved_asset", changed_file, {"source_path": changed_file}
                    )
                )
                continue
            urn, strategy, previous_fields = resolved
            added_fields: tuple[str, ...] = ()
            removed_fields: tuple[str, ...] = ()
            referenced_fields: tuple[FieldRef, ...] = ()
            file_path = self.repo_root / changed_file
            if changed_file.lower().endswith(".sql"):
                try:
                    projected_fields, referenced_fields = _sql_parts(
                        file_path.read_text(encoding="utf-8"), urn, convention
                    )
                    # ``--file`` evaluates the working tree against the checked-in
                    # dbt manifest: its column set is the pre-change contract.
                    if previous_fields:
                        added_fields = tuple(
                            sorted(set(projected_fields) - set(previous_fields))
                        )
                        removed_fields = tuple(
                            sorted(set(previous_fields) - set(projected_fields))
                        )
                    else:
                        added_fields = projected_fields
                except Exception as error:  # noqa: BLE001
                    # Parsing and filesystem failures are evidence, never a crash.
                    evidence.append(
                        Evidence(
                            "unparseable_sql",
                            urn,
                            {
                                "source_path": changed_file,
                                "error": type(error).__name__,
                            },
                        )
                    )
            assets.append(
                TouchedAsset(
                    urn=urn,
                    source_path=changed_file,
                    added_fields=added_fields,
                    removed_fields=removed_fields,
                    referenced_fields=referenced_fields,
                    resolution_strategy=strategy,
                )
            )
        return ResolutionResult(tuple(assets), tuple(evidence))


def resolve_changed_files(
    changed_files: Sequence[str | Path],
    *,
    repo_root: str | Path = ".",
    manifest_path: str | Path | None = None,
    assets_path: str | Path | None = None,
    naming_convention: NamingConvention | None = None,
) -> ResolutionResult:
    """Convenience entry point for callers that do not need a persistent Resolver."""
    return Resolver(
        repo_root,
        manifest_path=manifest_path,
        assets_path=assets_path,
        naming_convention=naming_convention,
    ).resolve(changed_files)


resolve_files = resolve_changed_files
