"""Deterministic MCP surface over the existing Sidq engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from sidq import cli
from sidq.gates.lineage_rot import LineageRotGate
from sidq.gates.reality import RealityGate
from sidq.graph.client import (
    DatasetInfo,
    GraphClient,
    MCPGraphClient,
    SchemaField,
    StdioMCPToolCaller,
)
from sidq.graph.live_source import LiveSourceClient
from sidq.models import Evidence, TouchedAsset
from sidq.resolver import Resolver, dataset_urn
from sidq.serialization import canonical_data, canonical_json

CHECK_CHANGE_DESCRIPTION = (
    "Call this before proposing or editing data SQL. It answers whether a proposed "
    "unified diff or SQL statement is allowed by the configured Sidq policy, using "
    "the full resolver, graph gates, and policy engine. Provide exactly one of diff "
    "or sql. Treat BLOCK as a stop signal, and never treat an error as permission."
)
VERIFY_CONTEXT_DESCRIPTION = (
    "Call this before trusting catalog metadata for an asset or using that asset in "
    "a query. It checks the catalog against the live schema and the model SQL against "
    "stored column lineage when those checks are available. truthful is true only "
    "when every check was completed without a finding; unavailable checks are listed "
    "in unverifiable, so absence of evidence is never presented as proof."
)
SEARCH_VERIFIED_DESCRIPTION = (
    "Call this when choosing catalog assets for analysis or code generation. It "
    "searches the catalog and returns assets with a truthful Sidq verification inside "
    "the requested age window in verified. Assets never checked, expired, or not "
    "fully checkable are reported in unverified; assets checked and found false are "
    "reported in rejected. A graph error is explicit and must not be treated as an "
    "empty successful search."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorInfo(_Model):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class EvidenceResult(_Model):
    kind: str
    subject: str
    detail: dict[str, Any]
    graph_links: list[str]


class FindingResult(_Model):
    rule_id: str
    severity: str
    message: str
    evidence: list[EvidenceResult]


class FieldRefResult(_Model):
    dataset_urn: str
    field_path: str


class TouchedAssetResult(_Model):
    urn: str
    source_path: str
    added_fields: list[str]
    removed_fields: list[str]
    referenced_fields: list[FieldRefResult]
    resolution_strategy: str


class VerdictResult(_Model):
    decision: Literal["PASS", "WARN", "BLOCK"]
    reason_code: str | None
    findings: list[FindingResult]
    touched: list[TouchedAssetResult]
    commit_sha: str
    policy_hash: str


class UnverifiableResult(_Model):
    check: Literal["schema_drift", "lineage_rot"]
    reason: str
    subject: str | None = None


class VerifyContextResult(_Model):
    urn: str
    truthful: bool
    findings: list[EvidenceResult]
    checked_at: str
    unverifiable: list[UnverifiableResult]
    error: ErrorInfo | None = None


class SearchAssetResult(_Model):
    urn: str
    verdict: Literal["TRUTHFUL", "UNTRUTHFUL", "UNVERIFIABLE"]
    checked_at: str
    truthful: bool


class UnverifiedAssetResult(_Model):
    urn: str
    status: Literal["unverified", "stale", "unverifiable"]
    reason: str
    checked_at: str | None = None


class SearchVerifiedResult(_Model):
    query: str
    max_age_days: int
    verified: list[SearchAssetResult]
    rejected: list[SearchAssetResult]
    unverified: list[UnverifiedAssetResult]
    error: ErrorInfo | None = None


class CheckChangeOutput(_Model):
    """A full verdict on success, or an explicit fail-closed error."""

    decision: Literal["PASS", "WARN", "BLOCK"] | None = None
    reason_code: str | None = None
    findings: list[FindingResult] | None = None
    touched: list[TouchedAssetResult] | None = None
    commit_sha: str | None = None
    policy_hash: str | None = None
    error: ErrorInfo | None = None
    verdict: VerdictResult | None = None


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    urn: str
    source_path: str
    project_root: Path
    manifest_path: Path


class VerificationStore:
    """Small canonical receipt store consumed by ``search_verified``."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).resolve() if path is not None else None
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = threading.RLock()

    def get(self, urn: str) -> dict[str, Any] | None:
        with self._lock:
            self._load()
            record = self._records.get(urn)
            return canonical_data(record) if record is not None else None

    def put(self, result: Mapping[str, Any]) -> None:
        urn = result.get("urn")
        if not isinstance(urn, str):
            raise TypeError("verification result has no URN")
        with self._lock:
            self._load()
            self._records[urn] = canonical_data(dict(result))
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = canonical_json({"records": self._records, "version": 1})
                descriptor, temporary = tempfile.mkstemp(
                    dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
                )
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(payload)
                        output.write(b"\n")
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None or not self.path.is_file():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = document.get("records") if isinstance(document, Mapping) else None
        if isinstance(records, Mapping):
            self._records = {
                str(urn): canonical_data(record)
                for urn, record in records.items()
                if isinstance(record, Mapping)
            }


class SidqService:
    """Synchronous application service shared by all three MCP tools."""

    def __init__(
        self,
        graph: GraphClient,
        *,
        live_source: LiveSourceClient | None = None,
        repo_root: str | Path = ".",
        store: VerificationStore | None = None,
        clock: Clock | None = None,
        default_sql_path: str | None = None,
    ) -> None:
        self.graph = graph
        self.live_source = live_source
        self.repo_root = Path(repo_root).resolve()
        self.store = store or VerificationStore()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.default_sql_path = default_sql_path

    def close(self) -> None:
        close = getattr(self.graph, "close", None)
        if callable(close):
            close()

    def check_change(
        self, *, diff: str | None, sql: str | None, policy_path: str | None
    ) -> dict[str, Any]:
        if (diff is None) == (sql is None):
            return _error_result(
                "INVALID_INPUT",
                "Provide exactly one of diff or sql.",
            )
        try:
            if sql is not None:
                with self._sql_workspace(sql) as (root, files):
                    verdict = cli.check(
                        files,
                        policy_path=policy_path,
                        graph=self.graph,
                        live_source=self.live_source,
                        repo_root=root,
                        commit_sha=_content_sha(sql),
                    )
            else:
                assert diff is not None
                with self._diff_workspace(diff) as (root, files):
                    verdict = cli.check(
                        files,
                        policy_path=policy_path,
                        graph=self.graph,
                        live_source=self.live_source,
                        repo_root=root,
                        commit_sha=_content_sha(diff),
                    )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return _error_result(
                "INVALID_INPUT",
                str(error) or type(error).__name__,
            )
        except Exception as error:  # noqa: BLE001 - MCP transports raise several exception types
            return _error_result(
                "ENGINE_ERROR",
                "Sidq could not complete the change check.",
                {"type": type(error).__name__},
            )

        result = canonical_data(verdict)
        graph_subjects = sorted(
            {
                evidence["subject"]
                for finding in result["findings"]
                for evidence in finding["evidence"]
                if evidence["kind"] == "graph_unavailable"
            }
        )
        if graph_subjects:
            return _error_result(
                "GRAPH_UNAVAILABLE",
                "The catalog graph could not be checked; no permission was granted.",
                {"subjects": graph_subjects},
                verdict=result,
            )
        return result

    def verify_context(self, urn: str) -> dict[str, Any]:
        checked_at = _timestamp(self.clock())
        findings: list[Evidence] = []
        unverifiable: list[dict[str, str]] = []
        error: dict[str, Any] | None = None

        try:
            dataset = self.graph.get_dataset(urn)
        except Exception as graph_error:  # noqa: BLE001
            dataset = None
            error = _error_info(
                "GRAPH_UNAVAILABLE",
                "The catalog graph could not be read; truth was not established.",
                {"type": type(graph_error).__name__},
            )
        if error is None and dataset is None:
            error = _error_info(
                "ASSET_NOT_FOUND",
                "The requested asset was not found in the catalog.",
                {"urn": urn},
            )

        asset = TouchedAsset(urn, "", (), (), ())
        if error is None:
            if self.live_source is None:
                unverifiable.append(
                    {
                        "check": "schema_drift",
                        "reason": "live source is not configured",
                    }
                )
            else:
                schema_evidence = RealityGate(self.live_source).collect(
                    [asset], self.graph
                )
                findings.extend(schema_evidence)
                if any(item.kind == "graph_unavailable" for item in schema_evidence):
                    error = _error_info(
                        "GRAPH_UNAVAILABLE",
                        "The schema-drift check could not read the graph or live source.",
                        {"subjects": sorted(item.subject for item in schema_evidence)},
                    )

            entry = self._manifest_entry(urn)
            if entry is None:
                unverifiable.append(
                    {
                        "check": "lineage_rot",
                        "reason": "no local model SQL is mapped to this catalog URN",
                    }
                )
            else:
                lineage_evidence = self._lineage_evidence(entry)
                for item in lineage_evidence:
                    if item.kind == "lineage_unverifiable":
                        unverifiable.append(
                            {
                                "check": "lineage_rot",
                                "reason": str(
                                    item.detail.get("reason", "check unavailable")
                                ),
                                "subject": item.subject,
                            }
                        )
                    else:
                        findings.append(item)
                if any(item.kind == "graph_unavailable" for item in lineage_evidence):
                    error = _error_info(
                        "GRAPH_UNAVAILABLE",
                        "The lineage-rot check could not read the graph.",
                        {
                            "subjects": sorted(
                                item.subject
                                for item in lineage_evidence
                                if item.kind == "graph_unavailable"
                            )
                        },
                    )

        truthful = error is None and not findings and not unverifiable
        result: dict[str, Any] = {
            "urn": urn,
            "truthful": truthful,
            "findings": findings,
            "checked_at": checked_at,
            "unverifiable": unverifiable,
        }
        if error is not None:
            result["error"] = error
        canonical = canonical_data(result)
        self.store.put(canonical)
        return canonical

    def search_verified(self, query: str, max_age_days: int) -> dict[str, Any]:
        if max_age_days < 0:
            return {
                "query": query,
                "max_age_days": max_age_days,
                "verified": [],
                "rejected": [],
                "unverified": [],
                "error": _error_info(
                    "INVALID_INPUT", "max_age_days must be zero or greater."
                ),
            }
        try:
            urns = _search_urns(self.graph, query)
        except Exception as graph_error:  # noqa: BLE001
            return {
                "query": query,
                "max_age_days": max_age_days,
                "verified": [],
                "rejected": [],
                "unverified": [],
                "error": _error_info(
                    "GRAPH_UNAVAILABLE",
                    "The catalog search failed; this is not an empty successful result.",
                    {"type": type(graph_error).__name__},
                ),
            }

        now = _utc(self.clock())
        cutoff = now - timedelta(days=max_age_days)
        verified: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        unverified: list[dict[str, Any]] = []
        for urn in urns:
            record = self.store.get(urn)
            if record is None:
                unverified.append(
                    {
                        "urn": urn,
                        "status": "unverified",
                        "reason": "never_checked",
                    }
                )
                continue
            checked_at = str(record.get("checked_at", ""))
            checked = _parse_timestamp(checked_at)
            if checked is None or checked < cutoff:
                unverified.append(
                    {
                        "urn": urn,
                        "status": "stale",
                        "reason": "verification_expired",
                        "checked_at": checked_at or None,
                    }
                )
                continue
            has_error = isinstance(record.get("error"), Mapping)
            gaps = record.get("unverifiable")
            has_gaps = isinstance(gaps, list) and bool(gaps)
            truthful = record.get("truthful") is True
            if has_error or has_gaps:
                unverified.append(
                    {
                        "urn": urn,
                        "status": "unverifiable",
                        "reason": "truth_check_incomplete",
                        "checked_at": checked_at,
                    }
                )
                continue
            item = {
                "urn": urn,
                "verdict": "TRUTHFUL" if truthful else "UNTRUTHFUL",
                "checked_at": checked_at,
                "truthful": truthful,
            }
            (verified if truthful else rejected).append(item)
        return canonical_data(
            {
                "query": query,
                "max_age_days": max_age_days,
                "verified": verified,
                "rejected": rejected,
                "unverified": unverified,
            }
        )

    def _lineage_evidence(self, entry: _ManifestEntry) -> list[Evidence]:
        resolved = Resolver(entry.project_root).resolve([entry.source_path])
        asset = next(
            (item for item in resolved.touched_assets if item.urn == entry.urn),
            None,
        )
        if asset is None:
            reason = (
                resolved.evidence[0].detail.get("error", "model could not be resolved")
                if resolved.evidence
                else "model could not be resolved"
            )
            return [
                Evidence(
                    "lineage_unverifiable",
                    entry.urn,
                    {"reason": str(reason), "confidence": "none"},
                )
            ]
        try:
            sql = (entry.project_root / entry.source_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as read_error:
            return [
                Evidence(
                    "lineage_unverifiable",
                    entry.urn,
                    {
                        "reason": f"model SQL could not be read: {type(read_error).__name__}",
                        "confidence": "none",
                    },
                )
            ]
        return LineageRotGate({entry.urn: sql}).collect([asset], self.graph)

    def _manifest_entry(self, urn: str) -> _ManifestEntry | None:
        return next(
            (entry for entry in _manifest_entries(self.repo_root) if entry.urn == urn),
            None,
        )

    def _sql_workspace(self, sql: str):
        entries = _manifest_entries(self.repo_root)
        entry = _select_sql_entry(entries, self.default_sql_path)
        return _temporary_project(entry, {entry.source_path: sql})

    def _diff_workspace(self, diff: str):
        patches = _parse_unified_diff(diff)
        if not patches:
            raise ValueError(
                "diff must be a unified diff containing at least one SQL file"
            )
        entries = _manifest_entries(self.repo_root)
        selected: list[tuple[_ManifestEntry, _Patch]] = []
        for patch in patches:
            entry = _entry_for_repo_path(entries, self.repo_root, patch.path)
            if entry is None:
                raise ValueError(f"no manifest entry maps changed file {patch.path}")
            selected.append((entry, patch))
        project_roots = {entry.project_root for entry, _ in selected}
        if len(project_roots) != 1:
            raise ValueError(
                "all changed SQL files must belong to one resolver project"
            )
        contents = {
            entry.source_path: _apply_patch(
                (self.repo_root / patch.path).read_text(encoding="utf-8"),
                patch,
            )
            for entry, patch in selected
        }
        return _temporary_project(selected[0][0], contents)


def create_server(service: SidqService) -> MCPServer:
    """Create a server with exactly the three public Sidq tools."""
    server = MCPServer(
        "sidq",
        title="Sidq catalog truth gate",
        description="Deterministic catalog truth checks and change policy for data agents.",
        instructions=(
            "Verify catalog context before trusting it. Check proposed data changes before "
            "writing them. Never interpret an error or unverifiable result as permission."
        ),
        version="0.1.0",
    )
    read_only = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )

    @server.tool(
        name="check_change",
        description=CHECK_CHANGE_DESCRIPTION,
        annotations=read_only,
        structured_output=True,
    )
    def check_change(
        diff: Annotated[
            str | None,
            Field(description="A unified diff containing proposed SQL file changes."),
        ] = None,
        sql: Annotated[
            str | None,
            Field(
                description="The complete proposed SQL text for one manifest-mapped model."
            ),
        ] = None,
        policy_path: Annotated[
            str | None,
            Field(
                description="Optional path to a Sidq policy YAML file; defaults to the built-in policy."
            ),
        ] = None,
    ) -> Annotated[CallToolResult, CheckChangeOutput]:
        return _tool_result(
            service.check_change(diff=diff, sql=sql, policy_path=policy_path)
        )

    @server.tool(
        name="verify_context",
        description=VERIFY_CONTEXT_DESCRIPTION,
        annotations=read_only,
        structured_output=True,
    )
    def verify_context(
        urn: Annotated[
            str,
            Field(
                description="The full DataHub dataset URN whose catalog claims must be checked."
            ),
        ],
    ) -> Annotated[CallToolResult, VerifyContextResult]:
        return _tool_result(service.verify_context(urn))

    @server.tool(
        name="search_verified",
        description=SEARCH_VERIFIED_DESCRIPTION,
        annotations=read_only,
        structured_output=True,
    )
    def search_verified(
        query: Annotated[
            str,
            Field(description="Catalog search text, such as customers or revenue."),
        ],
        max_age_days: Annotated[
            int,
            Field(
                ge=0,
                description="Maximum age in whole days for a verification to remain usable.",
            ),
        ] = 7,
    ) -> Annotated[CallToolResult, SearchVerifiedResult]:
        return _tool_result(service.search_verified(query, max_age_days))

    return server


def main() -> int:
    """Run the production Sidq MCP server on stdio."""
    service = _service_from_environment()
    try:
        create_server(service).run("stdio")
    finally:
        service.close()
    return 0


def _service_from_environment() -> SidqService:
    repo_root = Path(os.environ.get("SIDQ_REPO_ROOT", ".")).resolve()
    store_path = os.environ.get(
        "SIDQ_VERIFICATION_STORE",
        str(repo_root / ".sidq" / "mcp-verifications.json"),
    )
    live_source = (
        _PsqlLiveSource(os.environ["SIDQ_POSTGRES_DSN"])
        if os.environ.get("SIDQ_POSTGRES_DSN")
        else None
    )
    return SidqService(
        MCPGraphClient(StdioMCPToolCaller()),
        live_source=live_source,
        repo_root=repo_root,
        store=VerificationStore(store_path),
        default_sql_path=os.environ.get("SIDQ_SQL_PATH"),
    )


class _PsqlLiveSource:
    """Read a PostgreSQL schema with the standard ``psql`` client."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        relation = _postgres_relation(urn)
        if relation is None:
            return None
        schema, table = relation
        schema_literal = schema.replace("'", "''")
        table_literal = table.replace("'", "''")
        query = (
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            f"WHERE table_schema = '{schema_literal}' AND table_name = '{table_literal}' "
            "ORDER BY ordinal_position"
        )
        completed = subprocess.run(
            [
                "psql",
                self._dsn,
                "-X",
                "--no-align",
                "--tuples-only",
                "--field-separator",
                "\t",
                "--set",
                "ON_ERROR_STOP=1",
                "--command",
                query,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = [
            line.split("\t") for line in completed.stdout.splitlines() if line.strip()
        ]
        if not rows:
            return None
        return DatasetInfo(
            urn,
            tuple(
                SchemaField(name, native_type, nullable.upper() == "YES")
                for name, native_type, nullable in rows
            ),
        )


def _tool_result(value: Any) -> CallToolResult:
    payload = canonical_json(value)
    structured = json.loads(payload)
    return CallToolResult(
        content=[TextContent(type="text", text=payload.decode("utf-8"))],
        structured_content=structured,
    )


def _error_info(
    code: str, message: str, details: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "details": canonical_data(dict(details or {})),
    }


def _error_result(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    *,
    verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"error": _error_info(code, message, details)}
    if verdict is not None:
        result["verdict"] = canonical_data(verdict)
    return canonical_data(result)


def _content_sha(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _manifest_entries(repo_root: Path) -> list[_ManifestEntry]:
    entries: list[_ManifestEntry] = []
    manifests = sorted(
        path
        for path in repo_root.rglob("manifest.json")
        if not any(part in {".git", ".venv", "node_modules"} for part in path.parts)
    )
    for manifest in manifests:
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(document, Mapping):
            continue
        metadata = document.get("metadata")
        platform = (
            str(metadata.get("adapter_type", "dbt"))
            if isinstance(metadata, Mapping)
            else "dbt"
        )
        nodes = document.get("nodes")
        if not isinstance(nodes, Mapping):
            continue
        project_root = (
            manifest.parent.parent
            if manifest.parent.name == "target"
            else manifest.parent
        )
        for node in nodes.values():
            if not isinstance(node, Mapping):
                continue
            relation = node.get("relation_name")
            source_path = node.get("original_file_path", node.get("path"))
            if not isinstance(relation, str) or not isinstance(source_path, str):
                continue
            node_meta = node.get("meta")
            explicit_urn = (
                node_meta.get("dataset_urn") if isinstance(node_meta, Mapping) else None
            )
            config = node.get("config")
            config_meta = config.get("meta") if isinstance(config, Mapping) else None
            environment = (
                str(config_meta.get("environment", "PROD"))
                if isinstance(config_meta, Mapping)
                else "PROD"
            )
            urn = (
                explicit_urn
                if isinstance(explicit_urn, str)
                else dataset_urn(platform, relation, environment)
            )
            entries.append(
                _ManifestEntry(
                    urn,
                    Path(source_path).as_posix().lstrip("./"),
                    project_root.resolve(),
                    manifest.resolve(),
                )
            )
    return sorted(
        entries,
        key=lambda entry: (
            entry.urn,
            entry.project_root.as_posix(),
            entry.source_path,
        ),
    )


def _select_sql_entry(
    entries: Sequence[_ManifestEntry], default_sql_path: str | None
) -> _ManifestEntry:
    if default_sql_path is not None:
        wanted = Path(default_sql_path).as_posix().lstrip("./")
        matches = [
            entry
            for entry in entries
            if entry.source_path == wanted
            or (entry.project_root / entry.source_path).as_posix().endswith(wanted)
        ]
        if len(matches) == 1:
            return matches[0]
        raise ValueError("SIDQ_SQL_PATH must identify exactly one manifest model")
    if len(entries) != 1:
        raise ValueError(
            "raw sql requires SIDQ_SQL_PATH when the repository does not have exactly one manifest model"
        )
    return entries[0]


class _TemporaryProject:
    def __init__(self, entry: _ManifestEntry, contents: Mapping[str, str]) -> None:
        self.entry = entry
        self.contents = contents
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> tuple[Path, list[str]]:
        self._temporary = tempfile.TemporaryDirectory(prefix="sidq-mcp-")
        root = Path(self._temporary.name)
        relative_manifest = self.entry.manifest_path.relative_to(
            self.entry.project_root
        )
        destination_manifest = root / relative_manifest
        destination_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.entry.manifest_path, destination_manifest)
        assets = self.entry.project_root / ".sidq" / "assets.yml"
        if assets.is_file():
            destination_assets = root / ".sidq" / "assets.yml"
            destination_assets.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(assets, destination_assets)
        for source_path, sql in self.contents.items():
            destination = root / source_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(sql, encoding="utf-8")
        return root, sorted(self.contents)

    def __exit__(self, *_: object) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()


def _temporary_project(
    entry: _ManifestEntry, contents: Mapping[str, str]
) -> _TemporaryProject:
    return _TemporaryProject(entry, contents)


def _entry_for_repo_path(
    entries: Sequence[_ManifestEntry], repo_root: Path, path: str
) -> _ManifestEntry | None:
    absolute = (repo_root / path).resolve()
    matches = [
        entry
        for entry in entries
        if (entry.project_root / entry.source_path).resolve() == absolute
    ]
    return matches[0] if len(matches) == 1 else None


def _search_urns(graph: GraphClient, query: str) -> list[str]:
    search_assets = getattr(graph, "search_assets", None)
    if callable(search_assets):
        raw = search_assets(query)
    else:
        search = getattr(graph, "_search", None)
        if not callable(search):
            raise TypeError("graph client does not expose catalog search")
        raw = search(query)
    return sorted(set(_dataset_urns(raw)))


def _dataset_urns(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("urn:li:dataset:") else []
    if isinstance(value, Mapping):
        return [urn for item in value.values() for urn in _dataset_urns(item)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [urn for item in value for urn in _dataset_urns(item)]
    return []


def _postgres_relation(urn: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:postgres,([^,]+),[^)]+\)", urn
    )
    if match is None:
        return None
    parts = [part.strip('`"[] ') for part in match.group(1).split(".") if part]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Patch:
    path: str
    hunks: tuple[_Hunk, ...]


def _parse_unified_diff(diff: str) -> list[_Patch]:
    lines = diff.splitlines(keepends=True)
    patches: list[_Patch] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("+++ "):
            index += 1
            continue
        path = lines[index][4:].strip().split("\t", 1)[0]
        path = path.removeprefix("b/")
        index += 1
        hunks: list[_Hunk] = []
        while index < len(lines) and not lines[index].startswith("+++ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", lines[index])
            if match is None:
                if lines[index].startswith("diff --git ") or lines[index].startswith(
                    "--- "
                ):
                    break
                index += 1
                continue
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines):
                line = lines[index]
                if line.startswith(("@@ ", "diff --git ", "--- ")):
                    break
                if line.startswith((" ", "+", "-", "\\")):
                    hunk_lines.append(line)
                    index += 1
                    continue
                break
            hunks.append(_Hunk(int(match.group(1)), tuple(hunk_lines)))
        if path != "/dev/null" and path.lower().endswith(".sql") and hunks:
            patches.append(_Patch(path, tuple(hunks)))
    return patches


def _apply_patch(original: str, patch: _Patch) -> str:
    source = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in patch.hunks:
        start = max(hunk.old_start - 1, 0)
        if start < cursor or start > len(source):
            raise ValueError(f"diff hunk is out of range for {patch.path}")
        output.extend(source[cursor:start])
        cursor = start
        for line in hunk.lines:
            marker, content = line[:1], line[1:]
            if marker == "\\":
                continue
            if marker in {" ", "-"}:
                if cursor >= len(source) or source[cursor].rstrip(
                    "\n"
                ) != content.rstrip("\n"):
                    raise ValueError(f"diff context does not match {patch.path}")
                if marker == " ":
                    output.append(source[cursor])
                cursor += 1
            elif marker == "+":
                output.append(content)
    output.extend(source[cursor:])
    return "".join(output)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
