"""GitHub Action entry point for pull-request Sidq checks."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeGuard, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from sidq.bot.comment import STICKY_MARKER, render_comment
from sidq.cli import build_live_source_client, check
from sidq.graph.client import (
    GraphClient,
    MCPGraphClient,
    StdioMCPToolCaller,
)
from sidq.graph.fixtures import ReplayGraphClient
from sidq.models import Evidence, Finding, Verdict
from sidq.policy.engine import load_policy

_API_VERSION = "2022-11-28"
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_CHANGED_FILE_BYTES = 5 * 1024 * 1024
_EXIT_CODES = {"PASS": 0, "WARN": 1, "BLOCK": 2}
_CHECK_CONCLUSIONS = {0: "success", 1: "neutral", 2: "failure"}

type RunMode = Literal["live", "fixture"]


class ActionError(RuntimeError):
    """A safe-to-display action configuration or GitHub API failure."""


@dataclass(frozen=True, slots=True)
class PullRequestContext:
    number: int
    base_sha: str
    head_sha: str


class GitHubClient:
    """Small authenticated client for the four REST surfaces the bot needs."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
    ) -> None:
        parts = repository.split("/")
        if len(parts) != 2 or not all(parts):
            raise ActionError("GITHUB_REPOSITORY must have the form owner/repository")
        if not token:
            raise ActionError("a GitHub token is required")
        parsed = urlsplit(api_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ActionError("GITHUB_API_URL must be an HTTPS URL")
        self._repository_path = "/".join(quote(part, safe="") for part in parts)
        self._token = token
        self._api_url = api_url.rstrip("/")

    def list_pull_files(self, pull_number: int) -> list[str]:
        documents = self._paginate(
            f"/repos/{self._repository_path}/pulls/{pull_number}/files",
            maximum_pages=30,
        )
        files: list[str] = []
        for document in documents:
            filename = document.get("filename")
            if isinstance(filename, str):
                files.append(_safe_repo_path(filename))
        if len(documents) == 3000:
            raise ActionError(
                "GitHub returned its 3,000-file pull-request limit; refusing a "
                "partial verdict"
            )
        return sorted(set(files))

    def upsert_comment(self, pull_number: int, body: str) -> None:
        comments = self._paginate(
            f"/repos/{self._repository_path}/issues/{pull_number}/comments"
        )
        existing = next(
            (
                item
                for item in comments
                if STICKY_MARKER in str(item.get("body", ""))
                and _is_bot_comment(item)
                and isinstance(item.get("id"), int)
            ),
            None,
        )
        if existing is None:
            self._request(
                "POST",
                f"/repos/{self._repository_path}/issues/{pull_number}/comments",
                {"body": body},
            )
            return
        self._request(
            "PATCH",
            (f"/repos/{self._repository_path}/issues/comments/{existing['id']}"),
            {"body": body},
        )

    def create_check(
        self,
        *,
        head_sha: str,
        exit_code: int,
        decision: str,
        mode: RunMode,
    ) -> None:
        try:
            conclusion = _CHECK_CONCLUSIONS[exit_code]
        except KeyError as error:
            raise ActionError(f"unsupported Sidq exit code: {exit_code}") from error
        provenance = "recorded graph fixtures" if mode == "fixture" else "live DataHub"
        self._request(
            "POST",
            f"/repos/{self._repository_path}/check-runs",
            {
                "name": "Sidq policy verdict",
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "external_id": f"sidq:{head_sha}",
                "output": {
                    "title": f"Sidq: {decision}",
                    "summary": (
                        f"Decision {decision} (exit {exit_code}) using {provenance}."
                    ),
                },
            },
        )

    def _paginate(
        self, path: str, *, maximum_pages: int = 100
    ) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        for page in range(1, maximum_pages + 1):
            separator = "&" if "?" in path else "?"
            document = self._request(
                "GET",
                f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}",
            )
            if not isinstance(document, list):
                raise ActionError("GitHub returned an unexpected paginated response")
            page_items = [item for item in document if isinstance(item, Mapping)]
            items.extend(page_items)
            if len(document) < 100:
                return items
        return items

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        payload = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if body is not None
            else None
        )
        request = Request(
            f"{self._api_url}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "sidq-pr-bot/0.1",
                "X-GitHub-Api-Version": _API_VERSION,
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            raise ActionError(
                f"GitHub API {method} {path.split('?', 1)[0]} failed "
                f"with HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise ActionError(
                f"GitHub API {method} {path.split('?', 1)[0]} was unreachable"
            ) from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ActionError("GitHub API response exceeded the safe size limit")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise ActionError("GitHub returned invalid JSON") from error


def run_engine(
    files: Sequence[str],
    *,
    repo_root: Path,
    commit_sha: str,
    mode: RunMode,
    fixture_dir: Path | None = None,
    policy_path: Path | None = None,
) -> Verdict:
    """Run the existing Sidq engine, grouping files by their nearest manifest."""
    _validate_changed_files(files, repo_root)
    if mode == "fixture":
        if fixture_dir is None:
            raise ActionError("SIDQ_FIXTURE_DIR is required in fixture mode")
        graph: GraphClient = ReplayGraphClient(fixture_dir)
        owns_graph = False
    elif mode == "live":
        command = os.environ.get("SIDQ_DATAHUB_MCP_COMMAND", "").strip()
        if not command:
            command = str(Path(sys.executable).with_name("mcp-server-datahub"))
        graph = MCPGraphClient(StdioMCPToolCaller(command=command))
        owns_graph = True
    else:
        raise ActionError("SIDQ_MODE must be 'live' or 'fixture'")
    live_source = build_live_source_client()
    try:
        grouped: dict[Path, list[str]] = defaultdict(list)
        for filename in sorted(set(files)):
            root = _nearest_manifest_root(repo_root, filename)
            grouped[root].append((repo_root / filename).relative_to(root).as_posix())
        if not grouped:
            grouped[repo_root] = []
        verdicts = [
            check(
                grouped[root],
                policy_path=policy_path,
                graph=graph,
                live_source=live_source,
                repo_root=root,
                commit_sha=commit_sha,
            )
            for root in sorted(grouped, key=lambda item: item.as_posix())
        ]
        return _combine_verdicts(verdicts, commit_sha=commit_sha)
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()


def _combine_verdicts(verdicts: Sequence[Verdict], *, commit_sha: str) -> Verdict:
    if not verdicts:
        raise ActionError("Sidq produced no verdict")
    policy_hashes = {verdict.policy_hash for verdict in verdicts}
    if len(policy_hashes) != 1:
        raise ActionError("Sidq groups used different policy hashes")
    decision = max(verdicts, key=lambda item: _EXIT_CODES[item.decision]).decision
    reason_code = next(
        (
            verdict.reason_code
            for verdict in verdicts
            if verdict.decision == "BLOCK" and verdict.reason_code is not None
        ),
        None,
    )
    return Verdict(
        decision=decision,
        reason_code=reason_code,
        findings=tuple(finding for verdict in verdicts for finding in verdict.findings),
        touched=tuple(asset for verdict in verdicts for asset in verdict.touched),
        commit_sha=commit_sha,
        policy_hash=next(iter(policy_hashes)),
    )


def _nearest_manifest_root(repo_root: Path, filename: str) -> Path:
    candidate = repo_root / filename
    parents = (candidate.parent, *candidate.parents)
    for parent in parents:
        try:
            parent.relative_to(repo_root)
        except ValueError:
            continue
        manifests = (parent / "manifest.json", parent / "target" / "manifest.json")
        for manifest in manifests:
            if manifest.is_symlink():
                raise ActionError(
                    f"refusing symlinked dbt manifest: "
                    f"{manifest.relative_to(repo_root)}"
                )
            if manifest.is_file():
                return parent
        if parent == repo_root:
            break
    return repo_root


def _validate_changed_files(files: Sequence[str], repo_root: Path) -> None:
    for filename in files:
        safe_name = _safe_repo_path(filename)
        candidate = repo_root / safe_name
        if candidate.is_symlink():
            raise ActionError(f"refusing changed symlink: {safe_name}")
        if candidate.exists():
            try:
                candidate.resolve().relative_to(repo_root)
            except ValueError as error:
                raise ActionError(
                    f"changed file escapes repository: {safe_name}"
                ) from error
            if (
                candidate.is_file()
                and candidate.stat().st_size > _MAX_CHANGED_FILE_BYTES
            ):
                raise ActionError(
                    f"changed file exceeds {_MAX_CHANGED_FILE_BYTES} bytes: {safe_name}"
                )


def _safe_repo_path(filename: str) -> str:
    path = PurePosixPath(filename)
    if (
        not filename
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ActionError("GitHub returned an unsafe changed-file path")
    return path.as_posix()


def _is_bot_comment(comment: Mapping[str, Any]) -> bool:
    user = comment.get("user")
    if isinstance(user, Mapping) and user.get("type") == "Bot":
        return True
    return isinstance(comment.get("performed_via_github_app"), Mapping)


def _context_from_event(path: Path) -> PullRequestContext:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ActionError("GITHUB_EVENT_PATH is not readable JSON") from error
    pull = document.get("pull_request") if isinstance(document, Mapping) else None
    if not isinstance(pull, Mapping):
        raise ActionError("Sidq must run from a pull request event")
    number = pull.get("number", document.get("number"))
    base = pull.get("base")
    head = pull.get("head")
    base_sha = base.get("sha") if isinstance(base, Mapping) else None
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    if not isinstance(number, int) or number <= 0:
        raise ActionError("pull request number is missing")
    if not _valid_sha(base_sha) or not _valid_sha(head_sha):
        raise ActionError("pull request base/head SHA is invalid")
    return PullRequestContext(number, base_sha.lower(), head_sha.lower())


def _valid_sha(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", value))


def _workspace_root(workspace: Path, configured: str) -> Path:
    root = (workspace / configured).resolve()
    try:
        root.relative_to(workspace.resolve())
    except ValueError as error:
        raise ActionError("SIDQ_REPO_ROOT must stay inside GITHUB_WORKSPACE") from error
    if not root.is_dir():
        raise ActionError("SIDQ_REPO_ROOT is not a directory")
    return root


def _fixture_path(action_path: Path, configured: str) -> Path:
    path = (
        (Path.cwd() / configured).resolve()
        if configured
        else (action_path / "tests" / "fixtures" / "graph").resolve()
    )
    if not path.is_dir():
        raise ActionError(f"fixture directory does not exist: {path}")
    return path


def _policy_path(repo_root: Path, action_path: Path, configured: str) -> Path:
    if not configured:
        path = (
            action_path / "src" / "sidq" / "policy" / "default_policy.yaml"
        ).resolve()
        if not path.is_file():
            raise ActionError("trusted Sidq default policy is missing")
        return path
    relative = Path(configured)
    if relative.is_absolute():
        raise ActionError("SIDQ_POLICY must be relative to SIDQ_REPO_ROOT")
    path = (repo_root / relative).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as error:
        raise ActionError("SIDQ_POLICY must stay inside SIDQ_REPO_ROOT") from error
    if not path.is_file():
        raise ActionError(f"policy file does not exist: {configured}")
    return path


def _failure_verdict(
    *,
    mode: RunMode,
    commit_sha: str,
    policy_path: Path | None,
    error: Exception,
) -> Verdict:
    fixture = mode == "fixture"
    rule_id = "fixture_replay_failed" if fixture else "graph_check_failed"
    reason = (
        "Sidq could not complete the fixture replay, so this pull request is "
        "blocked closed."
        if fixture
        else "Sidq could not complete the live graph check, so this pull request "
        "is blocked closed."
    )
    try:
        policy_hash = load_policy(policy_path).policy_hash
    except Exception:  # noqa: BLE001 - the fallback must itself fail closed
        policy_hash = "unavailable"
    evidence = Evidence(
        kind=rule_id,
        subject="Sidq runtime",
        detail={"failure_type": type(error).__name__},
    )
    return Verdict(
        decision="BLOCK",
        reason_code="CHECK_FAILED",
        findings=(Finding(rule_id, "block", reason, (evidence,)),),
        touched=(),
        commit_sha=commit_sha,
        policy_hash=policy_hash,
    )


def _publish_result(
    client: GitHubClient,
    *,
    pull_number: int,
    head_sha: str,
    comment: str,
    exit_code: int,
    decision: str,
    mode: RunMode,
) -> None:
    failures: list[str] = []
    try:
        client.upsert_comment(pull_number, comment)
    except ActionError as error:
        failures.append(f"comment: {error}")
    try:
        client.create_check(
            head_sha=head_sha,
            exit_code=exit_code,
            decision=decision,
            mode=mode,
        )
    except ActionError as error:
        failures.append(f"check: {error}")
    if failures:
        raise ActionError("result publication failed (" + "; ".join(failures) + ")")


def main() -> int:
    try:
        workspace = Path(_required_env("GITHUB_WORKSPACE")).resolve()
        action_path = Path(os.environ.get("SIDQ_ACTION_PATH", workspace)).resolve()
        event = _context_from_event(Path(_required_env("GITHUB_EVENT_PATH")))
        mode_value = os.environ.get("SIDQ_MODE", "live").strip().lower()
        if mode_value not in {"live", "fixture"}:
            raise ActionError("SIDQ_MODE must be 'live' or 'fixture'")
        mode = cast(RunMode, mode_value)
        repo_root = _workspace_root(workspace, os.environ.get("SIDQ_REPO_ROOT", "."))
        policy_path = _policy_path(
            repo_root,
            action_path,
            os.environ.get("SIDQ_POLICY", "").strip(),
        )
        fixture_dir = (
            _fixture_path(action_path, os.environ.get("SIDQ_FIXTURE_DIR", "").strip())
            if mode == "fixture"
            else None
        )
        client = GitHubClient(
            _required_env("GITHUB_REPOSITORY"),
            _required_env("GITHUB_TOKEN"),
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        files = client.list_pull_files(event.number)
        try:
            verdict = run_engine(
                files,
                repo_root=repo_root,
                commit_sha=event.head_sha,
                mode=mode,
                fixture_dir=fixture_dir,
                policy_path=policy_path,
            )
        except Exception as error:  # noqa: BLE001 - engine failures block closed
            print(
                f"sidq: check failed closed ({type(error).__name__})",
                file=sys.stderr,
            )
            verdict = _failure_verdict(
                mode=mode,
                commit_sha=event.head_sha,
                policy_path=policy_path,
                error=error,
            )
        exit_code = _EXIT_CODES[verdict.decision]
        command = f"sidq check --diff {event.base_sha}...{event.head_sha} --json"
        _publish_result(
            client,
            pull_number=event.number,
            head_sha=event.head_sha,
            comment=render_comment(verdict, mode=mode, reproduce_command=command),
            exit_code=exit_code,
            decision=verdict.decision,
            mode=mode,
        )
        return exit_code
    except ActionError as error:
        print(f"sidq: {error}", file=sys.stderr)
        return 2


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ActionError(f"{name} is required")
    return value


if __name__ == "__main__":  # pragma: no cover - exercised as an action process
    raise SystemExit(main())
