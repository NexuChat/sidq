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

from sidq.agent import (
    CatalogAuditor,
    PriorReceipt,
    recall,
    receipts_for,
    render,
    render_writeback,
    write_receipts,
)
from sidq.agent.auditor import DEFAULT_BUDGET
from sidq.gates.base import Gate
from sidq.gates.blast import BlastRadiusGate
from sidq.gates.doc_rot import DocRotGate
from sidq.gates.governance import GovernanceGate
from sidq.gates.reality import RealityGate
from sidq.gates.schema import SchemaGate
from sidq.gates.self_contradiction import CatalogSnapshot
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
from sidq.receipt.read import get_verification_status, holds, render_verification
from sidq.receipt.write import StdioMCPReceiptToolCaller
from sidq.repair import (
    UNREPAIRABLE,
    apply_repairs,
    propose_all,
    prove,
    render_applied,
    render_plan,
    unfixed,
)
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
    # `doc_rot` and `governance` were built, tested, and then never wired to this
    # path: `sidq check` ran three gates while the README described both as things
    # Sidq checks. A capability that cannot fire is a claim, not a feature.
    #
    # Two gates are deliberately absent, and both exclusions were measured rather
    # than assumed:
    #
    # `self_contradiction` discards the change and audits the entire catalog, so
    # running it per pull request is the wrong scope. It belongs to the catalog
    # audit in `examples/03-catalog-truth-report/`, which is where it is called.
    #
    # `lineage_rot` was wired here and then removed. On the flagship example it
    # produced twenty-two `lineage_unverifiable` records, none adjudicable, which
    # the engine turns into twenty-two `informational` findings — flooding the
    # verdict a judge reads with noise and no signal. Suppressing the unverifiable
    # ones instead would violate the rule the whole product rests on: an
    # unperformed check must never be reported as a clean one. The policy already
    # settled the scope question by classifying `lineage_unverifiable` as a
    # catalog-health observation rather than a failure to inspect the change.
    # `verify_context` runs the gate properly, with manifest-mapped SQL.
    evidence: list[Evidence] = []
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
    audit_parser = commands.add_parser(
        "audit",
        help="audit a whole catalog for claims that contradict other claims",
    )
    audit_parser.add_argument(
        "--server", default="http://localhost:8080", help="DataHub GMS URL"
    )
    audit_parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help="how many assets to examine, most consequential first",
    )
    audit_parser.add_argument(
        "--via-mcp",
        action="store_true",
        help="read the catalog through the official DataHub MCP server, not the SDK",
    )
    audit_parser.add_argument(
        "--write-receipts",
        action="store_true",
        help="write a receipt back for every asset examined (off by default)",
    )
    audit_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "read the receipts previous runs wrote back and skip assets whose "
            "receipt still holds, so the budget reaches assets no run has seen"
        ),
    )
    audit_parser.add_argument("--json", action="store_true", dest="as_json")
    verify_parser = commands.add_parser(
        "verify",
        help="read one asset's receipt back from DataHub and judge whether it holds",
    )
    verify_parser.add_argument("urn", help="the dataset URN to read a receipt for")
    verify_parser.add_argument("--policy")
    verify_parser.add_argument("--json", action="store_true", dest="as_json")
    repair_parser = commands.add_parser(
        "repair",
        help="propose repairs the catalog's own evidence proves, and prove them",
    )
    repair_parser.add_argument(
        "--server", default="http://localhost:8080", help="DataHub GMS URL"
    )
    repair_parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    repair_parser.add_argument(
        "--via-mcp",
        action="store_true",
        help="read the catalog through the official DataHub MCP server, not the SDK",
    )
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="write the proven repairs (off by default; this mutates the catalog)",
    )
    repair_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _read_snapshot(arguments: Any) -> CatalogSnapshot | None:
    """Read the whole catalog, over MCP or the SDK. ``None`` means it was unreadable.

    `--via-mcp` reads through the official DataHub MCP server rather than the
    Python SDK. It is the difference between an agent that uses the agent surface
    and one that merely claims to: with it, a single run reads through official
    MCP, decides, and — with the write flags — writes back through official MCP
    too. Without it the SDK path is used, which sees the whole catalog rather than
    a bounded search page.
    """
    if arguments.via_mcp:
        graph_client: Any = MCPGraphClient(StdioMCPToolCaller())
        try:
            # Column lineage costs one MCP call per column, so it is resolved for
            # exactly the assets the auditor can afford to examine — the same
            # most-consumed-first ordering — and the rest are reported unresolved.
            snapshot = CatalogSnapshot.from_mcp(
                graph_client, field_lineage_budget=arguments.budget
            )
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            print(
                f"sidq: could not read the catalog over MCP: {error}", file=sys.stderr
            )
            return None
        finally:
            close = getattr(graph_client, "close", None)
            if callable(close):
                close()
        return snapshot

    try:
        from datahub.ingestion.graph.client import DataHubGraph
        from datahub.ingestion.graph.config import DatahubClientConfig
    except ImportError:
        print(
            "sidq: reading a whole catalog needs the DataHub client "
            "(pip install acryl-datahub), or pass --via-mcp",
            file=sys.stderr,
        )
        return None
    try:
        return CatalogSnapshot.from_datahub(
            DataHubGraph(DatahubClientConfig(server=arguments.server))
        )
    except Exception as error:  # noqa: BLE001 - the client raises several types
        print(f"sidq: could not read the catalog: {error}", file=sys.stderr)
        return None


def _audit(arguments: Any) -> int:
    """Point the auditor at a catalog and report what it found.

    Exit code is 1 when the catalog contradicts itself and 2 only when the catalog
    could not be read at all. An audit reports; it does not refuse a change, so
    reusing `check`'s BLOCK code for a finding would misrepresent what was run.
    """
    snapshot = _read_snapshot(arguments)
    if snapshot is None:
        return 2

    prior: dict[str, PriorReceipt] = {}
    if arguments.resume:
        # The memory lives in the catalog, so resuming is a read like any other.
        # If the receipts cannot be read, the prior stays empty and everything
        # is examined afresh — forgetting costs budget, never correctness.
        caller = StdioMCPReceiptToolCaller()
        try:
            policy_hash = PolicyEngine(None).decide((), commit_sha="").policy_hash
            prior = recall(
                [entity.urn for entity in snapshot.entities],
                caller,
                current_policy_hash=policy_hash,
            )
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            print(
                f"sidq: could not read prior receipts, re-examining everything: "
                f"{error}",
                file=sys.stderr,
            )
        finally:
            close = getattr(caller, "close", None)
            if callable(close):
                close()

    result = CatalogAuditor(snapshot, budget=arguments.budget, prior=prior).run()
    lines = list(render(result, catalog=arguments.server))

    if arguments.write_receipts:
        # Opt-in, because this mutates a catalog the operator may not own. The
        # receipt carries the policy's verdict, not the agent's opinion, and only
        # for assets the agent actually examined.
        caller = StdioMCPReceiptToolCaller()
        try:
            outcomes = write_receipts(
                receipts_for(result, commit_sha=commit_sha_for_ref("HEAD")), caller
            )
        finally:
            close = getattr(caller, "close", None)
            if callable(close):
                close()
        lines.extend(("", *render_writeback(outcomes)))

    if arguments.as_json:
        sys.stdout.buffer.write(canonical_json(result.summary()) + b"\n")
    else:
        print("\n".join(lines))
    return 1 if result.findings else 0


def _repair(arguments: Any) -> int:
    """Audit, propose repairs from catalog evidence, prove them, then optionally write.

    Exit code 1 means findings remain unrepaired, which is the normal outcome — most
    catalog contradictions have no mechanical fix and the agent says so rather than
    inventing one. 2 is reserved for a catalog that could not be read at all.
    """
    snapshot = _read_snapshot(arguments)
    if snapshot is None:
        return 2

    result = CatalogAuditor(snapshot, budget=arguments.budget).run()
    plan = prove(snapshot, propose_all(result.findings, snapshot))
    lines = render_plan(plan, dry_run=not arguments.apply)

    remaining = unfixed(result.findings, plan)
    if remaining:
        # Named, not counted away. The repairable checks are the minority, and a
        # report that showed only what it could fix would read as if the rest were
        # handled. Each unrepairable kind carries the reason it has no mechanical
        # fix, so "we did not repair this" never looks like "there was nothing here".
        lines.extend(("", f"Still standing, unrepaired: {len(remaining)}"))
        for kind in sorted({item.kind for item in remaining}):
            count = sum(1 for item in remaining if item.kind == kind)
            lines.append(f"  {kind:<34} {count}")
            reason = UNREPAIRABLE.get(kind)
            if reason:
                lines.append(f"    no mechanical repair: {reason}")

    if arguments.apply:
        caller = StdioMCPReceiptToolCaller()
        try:
            outcomes = apply_repairs(plan, caller, dry_run=False)
        finally:
            close = getattr(caller, "close", None)
            if callable(close):
                close()
        lines.extend(("", *render_applied(outcomes)))

    if arguments.as_json:
        sys.stdout.buffer.write(canonical_json(plan.summary()) + b"\n")
    else:
        print("\n".join(lines))
    return 1 if remaining else 0


def _verify(arguments: Any) -> int:
    """Read a receipt back through official MCP, in whatever process runs this.

    This is the last quarter of the loop, and it is a separate command on purpose.
    A writer that reports its own success proves nothing; the receipt is only worth
    something if an unrelated process can find it and reach the same conclusion.
    Staleness is computed here, never read from the catalog: the catalog stores
    what was decided, and whether that still applies is this reader's judgment.

    Exit code 1 means the receipt does not currently hold — absent, stale, or a
    recorded BLOCK. That is the answer, not a failure, so it stays distinct from
    the 2 returned when the catalog could not be read at all.
    """
    caller = StdioMCPReceiptToolCaller()
    try:
        policy_hash = (
            PolicyEngine(arguments.policy).decide((), commit_sha="").policy_hash
        )
        status = get_verification_status(
            arguments.urn, caller, current_policy_hash=policy_hash
        )
    except Exception as error:  # noqa: BLE001 - MCP transports raise several types
        print(f"sidq: could not read the receipt: {error}", file=sys.stderr)
        return 2
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()

    verified, _ = holds(status)
    if arguments.as_json:
        sys.stdout.buffer.write(
            canonical_json({**status, "verified": verified}) + b"\n"
        )
    else:
        print("\n".join(render_verification(arguments.urn, status)))
    return 0 if verified else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "audit":
        return _audit(arguments)
    if arguments.command == "verify":
        return _verify(arguments)
    if arguments.command == "repair":
        return _repair(arguments)
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
