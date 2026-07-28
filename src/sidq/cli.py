"""Command-line entry point over the same canonical Sidq artifact."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sidq.gates.blast import BlastRadiusGate
from sidq.gates.reality import RealityGate
from sidq.gates.schema import SchemaGate
from sidq.graph.client import DatasetInfo, GraphClient, LineagePath, LineageResult, MCPGraphClient, StdioMCPToolCaller
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

    def get_downstream(self, urn: str, depth: int, column: str | None = None) -> LineageResult:
        raise RuntimeError("graph client is not configured")

    def paths_between(self, a: str, b: str, source_column: str | None = None, target_column: str | None = None) -> list[LineagePath]:
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
    gates = [SchemaGate(), BlastRadiusGate()]
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
        return PolicyEngine(policy_path).decide(evidence, touched=resolved.touched_assets, commit_sha=commit_sha)
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()


def _resolver_root_and_files(files: Sequence[str], repo_root: str | Path) -> tuple[Path, list[str]]:
    """Use the nearest dbt manifest when a check is invoked from repository root."""
    root = Path(repo_root).resolve()
    if Path(repo_root) != Path(".") or len(files) != 1:
        return root, list(files)
    candidate = (root / files[0]).resolve()
    manifest_root = next(
        (parent for parent in (candidate.parent, *candidate.parents) if (parent / "manifest.json").is_file()),
        None,
    )
    if manifest_root is None or manifest_root == root:
        return root, list(files)
    try:
        return manifest_root, [candidate.relative_to(manifest_root).as_posix()]
    except ValueError:
        return root, list(files)


def _human(verdict: Verdict) -> str:
    rows = [(finding.severity.upper(), finding.rule_id, finding.message) for finding in verdict.findings]
    header = ("SEVERITY", "RULE", "MESSAGE")
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(3)]
    line = "  ".join(header[index].ljust(widths[index]) for index in range(3))
    separator = "  ".join("-" * width for width in widths)
    body = ["  ".join(row[index].ljust(widths[index]) for index in range(3)) for row in rows]
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
        rule = next((item for item in policy.rules if item.id == arguments.rule_id), None)
        if rule is None:
            print(f"Unknown rule: {arguments.rule_id}", file=sys.stderr)
            return 2
        print(rule.message)
        return 0
    try:
        files = [arguments.file] if arguments.file else changed_files(arguments.diff)
        verdict = check(files, policy_path=arguments.policy)
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
