"""Command-line entry point over the same canonical Sidq artifact."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sidq.gates.base import Gate
from sidq.gates.blast import BlastRadiusGate
from sidq.gates.doc_rot import DocRotGate
from sidq.gates.governance import GovernanceGate
from sidq.gates.reality import RealityGate
from sidq.gates.schema import SchemaGate
from sidq.graph.client import (
    DatasetInfo,
    GraphClient,
    LineagePath,
    LineageResult,
    MCPGraphClient,
    StdioMCPToolCaller,
)
from sidq.graph.live_source import LiveSourceClient
from sidq.models import Evidence, Verdict
from sidq.policy.engine import PolicyEngine, load_policy
from sidq.resolver import Resolver
from sidq.serialization import canonical_json


class _UnavailableClient:
    """Fail closed when a CLI caller has not supplied the live integration yet."""

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        raise RuntimeError("graph client is not configured")

    def find_dataset(self, name_or_urn: str) -> str | None:
        raise RuntimeError("graph client is not configured")

    def get_downstream(
        self, urn: str, depth: int, column: str | None = None
    ) -> LineageResult:
        raise RuntimeError("graph client is not configured")

    def paths_between(
        self,
        a: str,
        b: str,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> list[LineagePath]:
        raise RuntimeError("graph client is not configured")


def build_graph_client() -> GraphClient:
    """Build the live, read-only DataHub MCP client used by ``sidq check``."""
    return MCPGraphClient(StdioMCPToolCaller())


def build_live_source_client() -> LiveSourceClient | None:
    # The showcase graph is metadata-only. Gate 0 is intentionally run only in
    # the Postgres-backed scene, where its connection is supplied by that runner.
    return None


def changed_files(diff_range: str, *, cwd: str | Path = ".") -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", diff_range],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in completed.stdout.splitlines() if line]


def collect_evidence(
    touched: Sequence[Any], graph: GraphClient, live_source: LiveSourceClient | None
) -> list[Evidence]:
    evidence: list[Evidence] = []
    # Every gate the README advertises runs here. `doc_rot` and `governance` were
    # built, tested, and then never wired to any product surface: `sidq check` ran
    # three gates while the README described documentation rot and governance
    # evidence as things Sidq checks. A capability that cannot fire is a claim, not
    # a feature.
    gates: list[Gate] = [
        SchemaGate(),
        BlastRadiusGate(),
        GovernanceGate(),
        DocRotGate(),
    ]
    if live_source is not None:
        gates.insert(0, RealityGate(live_source))
    for gate in gates:
        evidence.extend(gate.collect(touched, graph))
    return evidence


def check(
    files: Sequence[str],
    *,
    policy_path: str | Path | None = None,
    graph: GraphClient | None = None,
    live_source: LiveSourceClient | None = None,
    repo_root: str | Path = ".",
    commit_sha: str = "",
) -> Verdict:
    root, resolved_files = _resolver_root_and_files(files, repo_root)
    resolved = Resolver(root).resolve(resolved_files)
    owns_graph = graph is None
    graph = graph or build_graph_client()
    live_source = live_source if live_source is not None else build_live_source_client()
    try:
        evidence = list(resolved.evidence)
        evidence.extend(collect_evidence(resolved.touched_assets, graph, live_source))
        return PolicyEngine(policy_path).decide(
            _with_graph_links(evidence),
            touched=resolved.touched_assets,
            commit_sha=commit_sha or commit_sha_for_ref("HEAD", repo_root=root),
        )
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()


def _resolver_root_and_files(
    files: Sequence[str], repo_root: str | Path
) -> tuple[Path, list[str]]:
    """Use the nearest dbt manifest when a check is invoked from repository root."""
    root = Path(repo_root).resolve()
    if Path(repo_root) != Path(".") or len(files) != 1:
        return root, list(files)
    candidate = (root / files[0]).resolve()
    manifest_root = next(
        (
            parent
            for parent in (candidate.parent, *candidate.parents)
            if (parent / "manifest.json").is_file()
        ),
        None,
    )
    if manifest_root is None or manifest_root == root:
        return root, list(files)
    try:
        return manifest_root, [candidate.relative_to(manifest_root).as_posix()]
    except ValueError:
        return root, list(files)


def _with_graph_links(evidence: Sequence[Evidence]) -> list[Evidence]:
    """Attach a directly usable DataHub UI link to every emitted evidence item."""
    datahub_ui_url = os.environ.get(
        "SIDQ_DATAHUB_UI_URL", "http://localhost:9002"
    ).rstrip("/")
    return [
        item
        if item.graph_links
        else replace(
            item,
            graph_links=(
                f"{datahub_ui_url}/dataset/{quote(item.subject.partition('#')[0], safe='')}",
            ),
        )
        for item in evidence
    ]


def commit_sha_for_ref(ref: str, *, repo_root: str | Path = ".") -> str:
    """Resolve the checked ref from Git metadata without shelling out to Git.

    The CLI already uses Git to enumerate a requested diff; this small reader avoids
    another command solely to populate the reproducibility field.  It understands
    loose and packed refs and deliberately returns an empty value when no full SHA
    can be proved from local metadata.
    """
    target = ref.strip()
    if "..." in target:
        target = target.rsplit("...", 1)[1]
    elif ".." in target:
        target = target.rsplit("..", 1)[1]
    target = target or "HEAD"
    git_dir = _git_dir(Path(repo_root).resolve())
    if git_dir is None:
        return ""
    if target == "HEAD":
        try:
            target = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if target.startswith("ref: "):
            target = target.removeprefix("ref: ").strip()
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", target):
        return target.lower()
    candidates = [target]
    if not target.startswith("refs/"):
        candidates.extend((f"refs/heads/{target}", f"refs/remotes/{target}"))
    for candidate in candidates:
        try:
            sha = (git_dir / candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
            return sha.lower()
    try:
        packed_refs = (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in packed_refs:
        parts = line.split()
        if (
            len(parts) == 2
            and parts[1] in candidates
            and re.fullmatch(r"[0-9a-fA-F]{40,64}", parts[0])
        ):
            return parts[0].lower()
    return ""


def _git_dir(root: Path) -> Path | None:
    candidate = root / ".git"
    if candidate.is_dir():
        return candidate
    if not candidate.is_file():
        return None
    try:
        pointer = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir: "):
        return None
    location = Path(pointer.removeprefix("gitdir: ").strip())
    return location if location.is_absolute() else (root / location).resolve()


def _human(verdict: Verdict) -> str:
    rows = [
        (finding.severity.upper(), finding.rule_id, finding.message)
        for finding in verdict.findings
    ]
    header = ("SEVERITY", "RULE", "MESSAGE")
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(3)]
    line = "  ".join(header[index].ljust(widths[index]) for index in range(3))
    separator = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(row[index].ljust(widths[index]) for index in range(3)) for row in rows
    ]
    return "\n".join([f"Sidq: {verdict.decision}", line, separator, *body])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sidq")
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check")
    group = check_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--diff", metavar="A..B")
    group.add_argument("--file", metavar="SQL")
    check_parser.add_argument("--policy")
    check_parser.add_argument("--json", action="store_true", dest="as_json")
    explain_parser = commands.add_parser("explain")
    explain_parser.add_argument("rule_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "explain":
        policy = load_policy()
        rule = next(
            (item for item in policy.rules if item.id == arguments.rule_id), None
        )
        if rule is None:
            print(f"Unknown rule: {arguments.rule_id}", file=sys.stderr)
            return 2
        print(rule.message)
        return 0
    try:
        files = [arguments.file] if arguments.file else changed_files(arguments.diff)
        ref = arguments.diff if arguments.diff else "HEAD"
        commit_sha = commit_sha_for_ref(ref)
        if not commit_sha:
            raise OSError(f"could not resolve a full commit SHA for {ref!r}")
        verdict = check(files, policy_path=arguments.policy, commit_sha=commit_sha)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"sidq: {error}", file=sys.stderr)
        return 2
    if arguments.as_json:
        sys.stdout.buffer.write(canonical_json(verdict) + b"\n")
    else:
        print(_human(verdict))
    return {"PASS": 0, "WARN": 1, "BLOCK": 2}[verdict.decision]


if __name__ == "__main__":  # pragma: no cover - module invocation only
    raise SystemExit(main())
