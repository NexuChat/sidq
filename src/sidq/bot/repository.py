"""Trusted-base publisher for this repository's maintenance/data boundary.

Never execute PR files or consume PR-produced artifacts in this process.
The generic ``sidq.bot.action`` remains an unconditional strict asset check.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sidq.bot.action import ActionError, GitHubClient, run_engine
from sidq.bot.applicability import Applicability, classify_changes
from sidq.bot.comment import STICKY_MARKER, render_comment
from sidq.bot.dependencies import validate_exports
from sidq.bot.validation import (
    StaleRun,
    ValidationContext,
    resolve_context,
    validate_checks,
)

REPOSITORY = "NexuChat/sidq"


def checkout_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def changed_records(
    client: GitHubClient, context: ValidationContext
) -> tuple[list[Mapping[str, Any]], int]:
    pull = client._request("GET", f"/repos/{REPOSITORY}/pulls/{context.number}")
    if (
        not isinstance(pull, Mapping)
        or pull.get("state") != "open"
        or pull.get("head", {}).get("sha") != context.head_sha
        or pull.get("base", {}).get("sha") != context.base_sha
    ):
        raise StaleRun("PR changed during assessment")
    # PR file lists and associations are mutable. Compare immutable SHAs instead;
    # GitHub returns at most 300 files on this endpoint, so its boundary refuses.
    document = client._request(
        "GET", f"/repos/{REPOSITORY}/compare/{context.base_sha}...{context.head_sha}"
    )
    if (
        not isinstance(document, Mapping)
        or document.get("base_commit", {}).get("sha") != context.base_sha
        or document.get("merge_base_commit", {}).get("sha") != context.base_sha
        or document.get("status") not in {"ahead", "identical"}
    ):
        raise ActionError(
            "immutable comparison is invalid or base is not contained in head"
        )
    files = document.get("files")
    if (
        not isinstance(files, list)
        or not 0 < len(files) < 300
        or any(not isinstance(item, Mapping) for item in files)
    ):
        raise ActionError("empty, malformed or potentially truncated immutable diff")
    return files, len(files)


def applicability_comment(
    scope: Applicability, context: ValidationContext, *, blocked: bool = False
) -> str:
    heading = (
        "BLOCKED — applicability not established"
        if blocked
        else "NOT APPLICABLE — no data-asset certification"
    )
    return (
        f"{STICKY_MARKER}\n# {heading}\n\n"
        f"{html.escape(scope.reason)}\n\n"
        "No graph verdict or asset receipt was produced. This does not certify "
        "source-code or dependency safety.\n\n"
        f"Categories: {', '.join(scope.categories) or 'unestablished'}.\n\n"
        f"Validation evidence: {context.run_url} "
        f"(attempt {context.run_attempt}).\n\n"
        f"Head: <code>{context.head_sha}</code> · "
        f"trusted base: <code>{context.base_sha}</code>\n"
    )


def publish(
    client: GitHubClient,
    context: ValidationContext,
    *,
    comment: str,
    conclusion: str,
    title: str,
) -> None:
    """Head/base-bound status; retries replace our check, never invent PASS."""
    external_id = f"sidq:{context.head_sha}:{context.base_sha}"
    document = client._request(
        "GET",
        f"/repos/{REPOSITORY}/commits/{context.head_sha}/check-runs?check_name=Sidq%20policy%20verdict&per_page=100",
    )
    if not isinstance(document, Mapping) or not isinstance(
        document.get("check_runs"), list
    ):
        raise ActionError("cannot inspect existing policy checks")
    if document.get("total_count", 0) >= 100:
        raise ActionError("policy check inventory is incomplete")
    matches = [
        item
        for item in document["check_runs"]
        if isinstance(item, Mapping)
        and item.get("name") == "Sidq policy verdict"
        and item.get("head_sha") == context.head_sha
        and item.get("external_id") == external_id
        and item.get("app", {}).get("slug") == "github-actions"
    ]
    if len(matches) > 1:
        raise ActionError("ambiguous existing policy checks")
    pending = {
        "name": "Sidq policy verdict",
        "head_sha": context.head_sha,
        "external_id": external_id,
        "status": "in_progress",
        "output": {
            "title": "Sidq: publishing evidence",
            "summary": "Publication is incomplete; no certification is available.",
        },
    }
    if matches:
        identifier = matches[0].get("id")
        if type(identifier) is not int or identifier <= 0:
            raise ActionError("invalid policy check ID")
        client._request(
            "PATCH",
            f"/repos/{REPOSITORY}/check-runs/{identifier}",
            {key: value for key, value in pending.items() if key != "head_sha"},
        )
    else:
        created = client._request("POST", f"/repos/{REPOSITORY}/check-runs", pending)
        identifier = created.get("id") if isinstance(created, Mapping) else None
        if type(identifier) is not int or identifier <= 0:
            raise ActionError("check creation did not return a valid ID")
    # Do not complete a success/neutral check if updating its evidence fails.
    client.upsert_comment(context.number, comment)
    client._request(
        "PATCH",
        f"/repos/{REPOSITORY}/check-runs/{identifier}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": comment},
        },
    )


def assess(
    client: GitHubClient,
    event: Mapping[str, Any],
    *,
    base_root: Path,
    head_root: Path,
) -> int:
    context = resolve_context(client, event, repository=REPOSITORY)
    try:
        if (
            checkout_sha(base_root) != context.base_sha
            or checkout_sha(head_root) != context.head_sha
        ):
            raise ActionError("checkout revision does not match validated PR context")
        validate_checks(client, context, repository=REPOSITORY)
        records, count = changed_records(client, context)
        scope = classify_changes(
            records, changed_count=count, base_root=base_root, head_root=head_root
        )
        if scope.kind == "non_data" and "dependencies" in scope.categories:
            # Do not delegate this prerequisite to editable PR test code.
            validate_exports(head_root)
        if scope.kind == "data":
            verdict = run_engine(
                scope.files,
                repo_root=head_root,
                commit_sha=context.head_sha,
                mode="fixture",
                fixture_dir=base_root / "tests/fixtures/graph",
                policy_path=base_root / "src/sidq/policy/default_policy.yaml",
            )
            comment = render_comment(
                verdict,
                mode="fixture",
                reproduce_command=f"sidq check --diff {context.base_sha}...{context.head_sha} --json",
            )
            conclusion = {"PASS": "success", "WARN": "neutral", "BLOCK": "failure"}[
                verdict.decision
            ]
            title = f"Sidq: {verdict.decision} (fixture replay)"
            result = 2 if verdict.decision == "BLOCK" else 0
        else:
            blocked = scope.kind == "blocked"
            comment = applicability_comment(scope, context, blocked=blocked)
            conclusion = "failure" if blocked else "neutral"
            title = (
                "Sidq: BLOCKED — applicability"
                if blocked
                else "Sidq: NOT APPLICABLE — no asset certification"
            )
            result = 2 if blocked else 0
        # Refuse old success after a newer run/attempt or PR revision appeared.
        current = resolve_context(client, event, repository=REPOSITORY)
        if current != context:
            raise StaleRun("validation context changed before publication")
        validate_checks(client, context, repository=REPOSITORY)
    except StaleRun:
        raise
    except Exception as error:
        current = resolve_context(client, event, repository=REPOSITORY)
        if current != context:
            raise StaleRun("PR changed before refusal publication") from error
        scope = Applicability(
            "blocked",
            (),
            (),
            f"Required verification could not complete ({type(error).__name__}).",
        )
        comment = applicability_comment(scope, context, blocked=True)
        conclusion, title, result = (
            "failure",
            "Sidq: BLOCKED — verification incomplete",
            2,
        )
    publish(client, context, comment=comment, conclusion=conclusion, title=title)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        if repository != REPOSITORY:
            raise ActionError("this maintenance scope is only for NexuChat/sidq")
        base_root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        if not isinstance(event, Mapping):
            raise ActionError("invalid workflow event")
        client = GitHubClient(repository, os.environ["GITHUB_TOKEN"])
        if args.prepare:
            context = resolve_context(client, event, repository=repository)
            if checkout_sha(base_root) != context.base_sha:
                raise StaleRun("trusted checkout no longer matches the PR base")
            if not re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", context.head_repository
            ):
                raise ActionError("invalid head repository")
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(
                    f"ready=true\nhead_sha={context.head_sha}\nhead_repository={context.head_repository}\n"
                )
            return 0
        return assess(
            client, event, base_root=base_root, head_root=base_root / ".sidq-pr"
        )
    except StaleRun as error:
        print(f"sidq: superseded/ineligible validation event ({error})")
        return 0
    except Exception as error:  # noqa: BLE001 - malformed inputs never produce N/A
        print(
            f"sidq: repository verification refused ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
