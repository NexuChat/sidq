"""Command-line entry point over the same canonical Sidq artifact."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
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
from sidq.agent.swarm import SwarmWorker, observe, render_worker
from sidq.agent.swarm_overlap import (
    SwarmReportError,
    discover_report_paths,
    load_reports,
    measure_overlap,
    write_worker_report,
)
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
from sidq.receipt.assertion import (
    AssertionMirrorUnavailable,
    emit_assertions,
    require_mirror_config,
)
from sidq.receipt.read import (
    get_verification_status,
    get_verification_statuses,
    render_verification,
)
from sidq.receipt.state import Action, judge
from sidq.receipt.write import RECEIPT_TOOLS, StdioMCPReceiptToolCaller
from sidq.repair import (
    REPAIR_TOOLS,
    UNREPAIRABLE,
    apply_repairs,
    propose_all,
    prove,
    refresh_snapshot,
    render_applied,
    render_plan,
    unfixed,
    verify_repairs,
)
from sidq.resolver import Resolver
from sidq.serialization import canonical_json

# Named here rather than reaching into the extractor, so `--model` with no value
# and `ModelExtractor()` cannot drift apart in a help string a judge reads.
_DEFAULT_CLAIM_MODEL = "ibm/granite4:1b-q4_1"


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
    for reference_dir in _git_reference_dirs(git_dir):
        for candidate in candidates:
            try:
                sha = (reference_dir / candidate).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
                return sha.lower()
        try:
            packed_refs = (
                (reference_dir / "packed-refs").read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            continue
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


def _git_reference_dirs(git_dir: Path) -> list[Path]:
    """Return the local and shared ref stores for a normal repo or worktree."""
    directories = [git_dir]
    try:
        pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return directories
    common = Path(pointer)
    directories.append(common if common.is_absolute() else (git_dir / common).resolve())
    return directories


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


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return parsed


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
        "--field-lineage",
        choices=("mcp", "aspect"),
        default="mcp",
        help=(
            "how to resolve column lineage: 'mcp' pays one get_lineage call per "
            "column through the agent surface; 'aspect' reads the stored "
            "upstreamLineage aspect once per dataset over DataHub's documented "
            "OpenAPI v3 (a different evidence boundary, see PROVENANCE-MATRIX.md)"
        ),
    )
    audit_parser.add_argument(
        "--write-receipts",
        action="store_true",
        help="write a receipt back for every asset examined (off by default)",
    )
    audit_parser.add_argument(
        "--write-assertions",
        action="store_true",
        help=(
            "also report receipt verdicts through DataHub native assertions "
            "(requires --write-receipts; off by default)"
        ),
    )
    audit_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "read the receipts previous runs wrote back and skip assets whose "
            "receipt still holds, so the budget reaches assets no run has seen"
        ),
    )
    audit_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait on the catalog before reporting it unreachable",
    )
    audit_parser.add_argument("--json", action="store_true", dest="as_json")
    claims_parser = commands.add_parser(
        "claims",
        help="test what the catalog's documentation asserts against the live source",
    )
    claims_parser.add_argument(
        "urn", nargs="+", help="dataset URNs whose documentation should be tested"
    )
    claims_parser.add_argument(
        "--server", default="http://localhost:8080", help="DataHub GMS URL"
    )
    claims_parser.add_argument(
        "--source",
        help=(
            "read-only PostgreSQL connection string for the live source "
            "(defaults to CLAIMS_SOURCE)"
        ),
    )
    reader_mode = claims_parser.add_mutually_exclusive_group()
    reader_mode.add_argument(
        "--model",
        nargs="?",
        const=_DEFAULT_CLAIM_MODEL,
        help=(
            "also read sentences the deterministic reader declined, using a local "
            "Ollama model. It proposes what to test and never what is true: a "
            "claim it proposes still has to survive read-only SQL against the "
            "source, and one that cannot be tested is dropped, not reported."
        ),
    )
    reader_mode.add_argument(
        "--reader",
        action="store_true",
        help=(
            "also read declined sentences with the trained multilingual reader "
            "in data/claims/reader/. Measured on a held-out split rather than "
            "asserted: see docs/CLAIM-READER.md. Needs `pip install 'sidq[reader]'`."
        ),
    )
    claims_parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="ignore model proposals below this confidence (rules are always 1.0)",
    )
    claims_parser.add_argument(
        "--budget", type=int, default=50, help="how many claims to test at most"
    )
    claims_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait on the catalog before reporting it unreachable",
    )
    claims_parser.add_argument("--json", action="store_true", dest="as_json")
    swarm_parser = commands.add_parser(
        "swarm",
        help="audit as one worker of a swarm, cooperating only through receipts",
    )
    swarm_parser.add_argument(
        "--worker-id",
        required=True,
        help="this worker's identity, recorded on every receipt it writes",
    )
    swarm_parser.add_argument(
        "--swarm-run",
        required=True,
        help="the run all workers of this swarm share, so a ledger can be read back",
    )
    swarm_parser.add_argument(
        "--server", default="http://localhost:8080", help="DataHub GMS URL"
    )
    swarm_parser.add_argument(
        "--budget", type=int, default=DEFAULT_BUDGET, help="assets this worker examines"
    )
    swarm_parser.add_argument(
        "--lineage-budget",
        type=int,
        default=0,
        help=(
            "assets whose column lineage the shared read resolves; defaults to "
            "four times --budget because a swarm's workers rotate across the plan "
            "and collectively cover far more ground than any one of them"
        ),
    )
    swarm_parser.add_argument(
        "--via-mcp",
        action="store_true",
        help="read the catalog through the official DataHub MCP server, not the SDK",
    )
    swarm_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait on the catalog before reporting it unreachable",
    )
    swarm_parser.add_argument(
        "--report",
        type=Path,
        help="atomically write this worker's own JSON account after it finishes",
    )
    swarm_parser.add_argument("--json", action="store_true", dest="as_json")

    overlap_parser = commands.add_parser(
        "swarm-overlap",
        help="measure duplicated examinations from surviving workers' own reports",
    )
    overlap_parser.add_argument(
        "report",
        nargs="+",
        type=Path,
        help="expected worker report paths, or a directory of JSON reports",
    )

    ledger_parser = commands.add_parser(
        "swarm-ledger",
        help="read a swarm's work back out of the catalog, trusting no worker's word",
    )
    ledger_parser.add_argument("--swarm-run", required=True)
    ledger_parser.add_argument(
        "--server", default="http://localhost:8080", help="DataHub GMS URL"
    )
    ledger_parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ledger_parser.add_argument("--via-mcp", action="store_true")
    ledger_parser.add_argument("--timeout", type=float, default=15.0)
    ledger_parser.add_argument("--json", action="store_true", dest="as_json")

    verify_parser = commands.add_parser(
        "verify",
        help="read one asset's receipt back from DataHub and judge whether it holds",
    )
    verify_parser.add_argument("urn", help="the dataset URN to read a receipt for")
    verify_parser.add_argument("--policy")
    verify_parser.add_argument(
        "--max-age-days",
        type=_nonnegative_int,
        default=7,
        help="maximum receipt age in days (default: 7)",
    )
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
    repair_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait on the catalog before reporting it unreachable",
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
            # `--field-lineage aspect` opts out of that cost by reading the whole
            # stored aspect once per dataset instead; it is a different evidence
            # boundary, not a faster route to the same one, so it is never the
            # default and the run says which one it used.
            reader: Any | None = None
            if arguments.field_lineage == "aspect":
                from sidq.graph.client import DataHubAspectClient

                reader = DataHubAspectClient(arguments.server)
            snapshot = CatalogSnapshot.from_mcp(
                graph_client,
                field_lineage_budget=arguments.budget,
                field_lineage_reader=reader,
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
            DataHubGraph(
                DatahubClientConfig(
                    server=arguments.server,
                    # The SDK's defaults retry a dead endpoint for minutes. A
                    # judge who mistypes a port deserves a refusal, not a hang:
                    # an unreachable catalog is an answer this tool can give in
                    # seconds, and giving it slowly looks like a broken tool.
                    timeout_sec=arguments.timeout,
                    retry_max_times=1,
                )
            )
        )
    except Exception as error:  # noqa: BLE001 - the client raises several types
        print(f"sidq: could not read the catalog: {error}", file=sys.stderr)
        return None


def _claims(arguments: Any) -> int:
    """Test what the catalog's documentation asserts against the live source.

    This is the one command where a model is allowed to participate, and the
    shape of that participation is the point: it proposes *what to test* on the
    sentences the deterministic reader would not commit to, and the verdict
    still comes from row counts returned by read-only SQL. `--model` is opt-in;
    without it the command runs on rules alone and produces the same verdicts,
    only from fewer sentences.
    """
    import json

    source = arguments.source or os.environ.get("CLAIMS_SOURCE")
    if not source or not source.strip():
        print(
            "sidq: claims requires --source or the CLAIMS_SOURCE environment variable",
            file=sys.stderr,
        )
        return 2

    from sidq.claims.attest import DocumentationAttester, datasets_from, render
    from sidq.claims.verify import ClaimVerifier
    from sidq.graph.live_source import PostgresLiveSourceClient

    try:
        import psycopg
    except ModuleNotFoundError:
        print(
            "sidq: this command reads a live PostgreSQL source; install the extra "
            "with `pip install 'sidq[live]'`",
            file=sys.stderr,
        )
        return 2

    # The documentation is read from the catalog through the official MCP server,
    # the same surface every other agent command uses. What it is tested against
    # is a different system entirely — that separation is the check.
    graph: Any = MCPGraphClient(StdioMCPToolCaller())
    try:
        datasets = datasets_from(graph, list(arguments.urn))
    finally:
        close = getattr(graph, "close", None)
        if callable(close):
            close()
    if not datasets:
        print("sidq: none of the named datasets could be read", file=sys.stderr)
        return 2

    # An unavailable reader is not a failed run. The rule-based reader still
    # covers every sentence it was ever going to cover, and saying so is more
    # useful than exiting with nothing done.
    extra: Any = None
    if arguments.reader:
        from sidq.claims.reader import EmbeddingClaimReader

        try:
            extra = EmbeddingClaimReader()
        except Exception as error:  # noqa: BLE001 - loading raises several types
            print(f"sidq: reader unavailable, rules only ({error})", file=sys.stderr)
    elif arguments.model:
        from sidq.claims.extractor import ModelExtractor

        try:
            extra = ModelExtractor(arguments.model)
        except Exception as error:  # noqa: BLE001 - the runtime raises its own types
            print(f"sidq: model unavailable, rules only ({error})", file=sys.stderr)

    def connect() -> Any:
        # Read-only by construction: every compiled claim is a SELECT, and the
        # session is set read-only as well so a mistake in compilation cannot
        # become a write against someone's warehouse.
        connection = psycopg.connect(source)
        connection.read_only = True
        return connection

    live_source = PostgresLiveSourceClient(connect)
    verifier = ClaimVerifier(live_source, connect)
    run = DocumentationAttester(
        verifier, extra=extra, min_confidence=arguments.min_confidence
    ).run(datasets, budget=arguments.budget)
    reader_identity: dict[str, object] | None = None
    if extra is not None:
        supplied_identity = getattr(extra, "identity", None)
        if isinstance(supplied_identity, dict):
            reader_identity = supplied_identity
        else:
            model_name = getattr(extra, "model", None)
            if isinstance(model_name, str):
                reader_identity = {"kind": "ollama", "model": model_name}

    evidence = run.evidence()
    verdict = PolicyEngine(None).decide(evidence, commit_sha=commit_sha_for_ref("HEAD"))
    if arguments.as_json:
        print(
            json.dumps(
                {
                    "summary": run.summary(),
                    "proposal_reader": reader_identity,
                    "decision": verdict.decision,
                    "policy_hash": verdict.policy_hash,
                    "findings": [
                        {
                            "urn": item.urn,
                            "column": item.claim.column,
                            "origin": item.claim.origin,
                            "sentence": item.claim.source_sentence,
                            "status": item.verification.status,
                            "violating_rows": item.verification.violating_row_count,
                        }
                        for item in run.admitted
                    ],
                },
                indent=2,
            )
        )
    else:
        print("\n".join(render(run, verdict.decision, reader_identity=reader_identity)))
    return 1 if verdict.decision == "BLOCK" else 0


def _audit(arguments: Any) -> int:
    """Point the auditor at a catalog and report what it found.

    Exit code is 1 when the catalog contradicts itself and 2 only when the catalog
    could not be read at all. An audit reports; it does not refuse a change, so
    reusing `check`'s BLOCK code for a finding would misrepresent what was run.
    """
    if getattr(arguments, "write_assertions", False):
        if not arguments.write_receipts:
            print("sidq: --write-assertions requires --write-receipts", file=sys.stderr)
            return 2
        # Before the catalog is read, not after receipts are already written.
        # The mirror needs its target catalog configured, and that is knowable
        # up front; it now runs from this project environment without an SDK.
        # Discovering a missing target at emission time would spend the whole
        # budget to arrive at a refusal it could have opened with.
        try:
            require_mirror_config()
        except AssertionMirrorUnavailable as error:
            print(f"sidq: {error}", file=sys.stderr)
            return 2

    snapshot = _read_snapshot(arguments)
    if snapshot is None:
        return 2

    prior: dict[str, PriorReceipt] = {}
    if arguments.resume:
        # The memory lives in the catalog, so resuming is a read like any other.
        # If the receipts cannot be read, the prior stays empty and everything
        # is examined afresh — forgetting costs budget, never correctness.
        caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
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
    outcomes: list[Any] = []
    receipts: list[Any] = []
    assertion_failed = False
    assertion_summary: dict[str, int] | None = None

    if arguments.write_receipts:
        # Opt-in, because this mutates a catalog the operator may not own. The
        # receipt carries the policy's verdict, not the agent's opinion, and only
        # for assets the agent actually examined.
        caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
        try:
            receipts = receipts_for(result, commit_sha=commit_sha_for_ref("HEAD"))
            outcomes = write_receipts(receipts, caller)
        finally:
            close = getattr(caller, "close", None)
            if callable(close):
                close()
        lines.extend(("", *render_writeback(outcomes)))

    if getattr(arguments, "write_assertions", False):
        successful_receipts = [
            receipt
            for receipt, outcome in zip(receipts, outcomes, strict=True)
            if outcome.written
        ]
        try:
            # No gms_url from --server. Under --via-mcp that flag is a display
            # label, not a connection target: reads and receipt writes go
            # through the MCP process, which resolves DATAHUB_GMS_URL itself.
            # Passing --server here would let assertions land in a different
            # catalog than the receipts they mirror.
            assertion_result = emit_assertions(successful_receipts)
        except Exception as error:  # noqa: BLE001 - DataHub transports raise several types
            # No counts here on purpose. Emission raises on the first failing
            # proposal, so an earlier assertion in the same run may already be
            # in the catalog; reporting zero would claim more than is known.
            assertion_failed = True
            print(f"sidq: could not write native assertions: {error}", file=sys.stderr)
            lines.append("  assertions        write failed, counts unknown")
        else:
            assertion_summary = {
                # `eligible` first, because zero runs from zero eligible
                # receipts is a catalog that rejected every write, not a clean
                # mirror, and the counts alone cannot tell those apart.
                "eligible": len(successful_receipts),
                "created": len(assertion_result["created"]),
                "existing": len(assertion_result["existing"]),
                "runs": len(assertion_result["runs"]),
                "retired": len(assertion_result["retired"]),
                "skipped": len(assertion_result["skipped"]),
            }
            lines.append(
                f"  assertion runs    {assertion_summary['runs']} "
                f"from {assertion_summary['eligible']} written receipts"
            )
            lines.append(
                f"  assertions        {assertion_summary['created']} new, "
                f"{assertion_summary['existing']} updated, "
                f"{assertion_summary['retired']} retired, "
                f"{assertion_summary['skipped']} left deleted"
            )

    if arguments.as_json:
        output = result.summary()
        if arguments.write_receipts:
            failed_outcomes = [item for item in outcomes if not item.written]
            output = {
                **output,
                "writes": {
                    "attempted": len(outcomes),
                    "written": len(outcomes) - len(failed_outcomes),
                    "failed": len(failed_outcomes),
                    "failures": [
                        {
                            "urn": item.urn,
                            "verdict": item.verdict,
                            "detail": item.detail,
                        }
                        for item in sorted(
                            failed_outcomes,
                            key=lambda item: (item.urn, item.verdict, item.detail),
                        )
                    ],
                },
            }
        if getattr(arguments, "write_assertions", False):
            # Only under the opt-in flag, so no-write JSON stays byte-identical.
            output = {
                **output,
                "assertions": (
                    {"failed": True}
                    if assertion_summary is None
                    else {**assertion_summary, "failed": False}
                ),
            }
        sys.stdout.buffer.write(canonical_json(output) + b"\n")
    else:
        print("\n".join(lines))
    write_failed = any(not item.written for item in outcomes)
    return 1 if result.findings or write_failed or assertion_failed else 0


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
    outcomes: list[Any] = []

    if arguments.apply:
        # Repair writes tags, terms, and owners on someone else's assets. It has
        # no business in the receipt namespace, so it is handed the proposal
        # tools alone — never RECEIPT_TOOLS.
        caller = StdioMCPReceiptToolCaller(REPAIR_TOOLS)
        try:
            outcomes = apply_repairs(plan, caller, dry_run=False)
        finally:
            close = getattr(caller, "close", None)
            if callable(close):
                close()
        if any(item.applied for item in outcomes):
            outcomes = verify_repairs(
                snapshot,
                outcomes,
                lambda proposals, timeout: _read_repair_targets(
                    snapshot,
                    proposals,
                    server=arguments.server,
                    timeout=timeout,
                ),
                timeout=arguments.timeout,
            )

    remaining = list(unfixed(result.findings, plan))
    failed_repairs = {
        (item.proposal.finding_kind, item.proposal.subject)
        for item in outcomes
        if not item.closed and not item.unresolved and not item.collateral
    }
    remaining_keys = {(item.kind, item.subject) for item in remaining}
    remaining.extend(
        item
        for item in result.findings
        if (item.kind, item.subject) in failed_repairs
        and (item.kind, item.subject) not in remaining_keys
    )
    unresolved = {
        (finding.kind, finding.subject): finding
        for outcome in outcomes
        for finding in outcome.unresolved
    }
    collateral = {
        (finding.kind, finding.subject): finding
        for outcome in outcomes
        for finding in outcome.collateral
    }
    remaining.extend(
        Evidence(
            finding.kind,
            finding.subject,
            {"repair_collateral": finding.detail} if finding.detail else {},
        )
        for key, finding in sorted({**unresolved, **collateral}.items())
        if key not in {(item.kind, item.subject) for item in remaining}
    )
    if remaining:
        # Named, not counted away. The repairable checks are the minority, and a
        # report that showed only what it could fix would read as if the rest were
        # handled. Each unrepairable kind carries the reason it has no mechanical
        # fix, so "we did not repair this" never looks like "there was nothing here".
        lines.extend(("", f"Still standing, unrepaired: {len(remaining)}"))
        for kind in sorted({item.kind for item in remaining}):
            count = sum(1 for item in remaining if item.kind == kind)
            lines.append(f"  {kind:<34} {count}")
            for item in sorted(
                (item for item in remaining if item.kind == kind),
                key=lambda item: item.subject,
            ):
                lines.append(f"    {item.subject}")
                detail = item.detail.get("repair_collateral")
                if detail:
                    lines.append(f"      {detail}")
            reason = UNREPAIRABLE.get(kind)
            if reason:
                lines.append(f"    no mechanical repair: {reason}")

    if arguments.apply:
        lines.extend(("", *render_applied(outcomes)))

    if arguments.as_json:
        output = plan.summary()
        if arguments.apply:
            written = sum(1 for item in outcomes if item.applied)
            verified = sum(1 for item in outcomes if item.closed)
            output = {
                **output,
                "remaining": {
                    "count": len(remaining),
                    "findings": [
                        {
                            "kind": item.kind,
                            "subject": item.subject,
                            **(
                                {"detail": item.detail["repair_collateral"]}
                                if item.detail.get("repair_collateral")
                                else {}
                            ),
                        }
                        for item in sorted(
                            remaining, key=lambda item: (item.kind, item.subject)
                        )
                    ],
                    "kinds": {
                        kind: sum(1 for item in remaining if item.kind == kind)
                        for kind in sorted({item.kind for item in remaining})
                    },
                },
                "writes": {
                    "attempted": len(outcomes),
                    "applied_unverified": written - verified,
                    "failed": sum(1 for item in outcomes if not item.applied),
                    "verified": verified,
                    "written": written,
                },
            }
        sys.stdout.buffer.write(canonical_json(output) + b"\n")
    else:
        print("\n".join(lines))
    write_unclosed = any(not item.closed for item in outcomes)
    return 1 if remaining or write_unclosed else 0


def _read_repair_targets(
    before: CatalogSnapshot,
    proposals: Sequence[Any],
    *,
    server: str,
    timeout: float,
) -> CatalogSnapshot:
    """Rebuild mutated state from direct entity reads, never from search."""
    import queue
    import threading

    completed: queue.Queue[tuple[CatalogSnapshot | None, Exception | None]] = (
        queue.Queue(maxsize=1)
    )

    def read() -> None:
        graph: Any = None
        try:
            graph = MCPGraphClient(StdioMCPToolCaller(gms_url=server))
            snapshot = refresh_snapshot(before, proposals, graph)
        except Exception as error:  # noqa: BLE001 - MCP transports raise several types
            completed.put((None, error))
        else:
            completed.put((snapshot, None))
        finally:
            close = getattr(graph, "close", None)
            if callable(close):
                close()

    worker = threading.Thread(target=read, name="sidq-repair-readback", daemon=True)
    worker.start()
    worker.join(timeout=max(timeout, 0.0))
    if worker.is_alive():
        raise TimeoutError("direct repair read exceeded its bounded timeout")
    snapshot, error = completed.get_nowait()
    if error is not None:
        raise error
    if snapshot is None:
        raise RuntimeError("direct repair read returned no snapshot")
    return snapshot


def replace_budget(arguments: Any, budget: int) -> Any:
    """A shallow view of the parsed arguments with a different read budget."""
    from copy import copy

    widened = copy(arguments)
    widened.budget = budget
    return widened


def _swarm(arguments: Any) -> int:
    """One worker of a swarm: read fresh, decide, write now, move on.

    Nothing here coordinates with the other workers. They are separate
    processes, possibly on separate machines, and the only thing they share is
    the catalog — so cooperation is whatever the receipts already in it say.
    """
    # Each worker enters the shared plan at its own offset, so the read must
    # resolve column lineage well past one worker's own budget — otherwise a
    # rotated worker lands on assets nothing fetched lineage for, correctly
    # reports that it could not establish anything, and writes no receipt. The
    # honesty rule is right; the read was simply too narrow for a swarm.
    lineage = arguments.lineage_budget or arguments.budget * 4
    snapshot = _read_snapshot(replace_budget(arguments, lineage))
    if snapshot is None:
        return 2

    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
    try:
        result = SwarmWorker(
            snapshot,
            worker_id=arguments.worker_id,
            swarm_run=arguments.swarm_run,
            tool_caller=caller,
            budget=arguments.budget,
            commit_sha=commit_sha_for_ref("HEAD"),
        ).run()
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()

    report_path = getattr(arguments, "report", None)
    if report_path is not None:
        try:
            write_worker_report(result, report_path)
        except OSError as error:
            print(f"sidq: could not write swarm report: {error}", file=sys.stderr)
            return 2

    if arguments.as_json:
        sys.stdout.buffer.write(canonical_json(result.summary()) + b"\n")
    else:
        print("\n".join(render_worker(result)))
    return 1 if result.findings or result.write_failures else 0


def _swarm_overlap(arguments: Any) -> int:
    """Measure overlap from worker accounts, never from DataHub receipts."""
    try:
        paths, expected_reports = discover_report_paths(arguments.report)
        reports = load_reports(paths)
        overlap = measure_overlap(reports, expected_reports=expected_reports)
    except (OSError, SwarmReportError) as error:
        print(f"sidq: could not measure swarm overlap: {error}", file=sys.stderr)
        return 2
    print("\n".join(overlap.render()))
    return 0


def _swarm_ledger(arguments: Any) -> int:
    """What the swarm did, read from the catalog rather than from the workers.

    A swarm that reported its own success would be the self-attestation this
    project refuses, so the ledger asks DataHub and nobody else.
    """
    snapshot = _read_snapshot(arguments)
    if snapshot is None:
        return 2

    urns = [entity.urn for entity in snapshot.entities]
    caller = StdioMCPToolCaller()
    try:
        statuses = get_verification_statuses(urns, caller)
    except Exception as error:  # noqa: BLE001 - MCP transports raise several types
        print(f"sidq: could not read the ledger: {error}", file=sys.stderr)
        return 2
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()

    report = observe(urns, statuses, swarm_run=arguments.swarm_run)
    if arguments.as_json:
        sys.stdout.buffer.write(canonical_json(report.summary()) + b"\n")
    else:
        print("\n".join(report.render()))
    return 0


def _verify(arguments: Any) -> int:
    """Read a receipt back through official MCP, in whatever process runs this.

    This is the last quarter of the loop, and it is a separate command on purpose.
    A writer that reports its own success proves nothing; the receipt is only worth
    something if an unrelated process can find it and reach the same conclusion.
    Staleness is computed here, never read from the catalog: the catalog stores
    what was decided, and whether that still applies is this reader's judgment.

    Exit code 1 means the caller may not proceed on this receipt — a current
    refusal, or nothing applicable to read (absent, stale, unreadable). Those are
    different answers and the output says which; they share an exit code because
    a script's only safe move in either case is to stop. Both stay distinct from
    the 2 returned when the catalog could not be read at all.
    """
    caller = StdioMCPToolCaller()
    try:
        policy_hash = (
            PolicyEngine(arguments.policy).decide((), commit_sha="").policy_hash
        )
        status = get_verification_status(
            arguments.urn,
            caller,
            current_policy_hash=policy_hash,
            max_age=timedelta(days=arguments.max_age_days),
        )
    except Exception as error:  # noqa: BLE001 - MCP transports raise several types
        print(f"sidq: could not read the receipt: {error}", file=sys.stderr)
        return 2
    finally:
        close = getattr(caller, "close", None)
        if callable(close):
            close()

    judgment = judge(status)
    if arguments.as_json:
        # Every axis is emitted separately. A consumer that wants "may I act"
        # reads `action`; one that wants "was this examined" reads
        # `covers_asset`; one that wants "does the receipt apply" reads
        # `receipt_state`. There is no single boolean to misread.
        sys.stdout.buffer.write(
            canonical_json({**status, **judgment.as_dict()}) + b"\n"
        )
    else:
        print("\n".join(render_verification(arguments.urn, status)))
    # WARN is not a refusal: it authorizes acting with review, so it does not
    # fail the command. STOP and RECHECK both do.
    return 0 if judgment.action in {Action.CONTINUE, Action.REVIEW} else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "audit":
        return _audit(arguments)
    if arguments.command == "claims":
        return _claims(arguments)
    if arguments.command == "verify":
        return _verify(arguments)
    if arguments.command == "repair":
        return _repair(arguments)
    if arguments.command == "swarm":
        return _swarm(arguments)
    if arguments.command == "swarm-overlap":
        return _swarm_overlap(arguments)
    if arguments.command == "swarm-ledger":
        return _swarm_ledger(arguments)
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
