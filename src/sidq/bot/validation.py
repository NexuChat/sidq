"""Authenticate mandatory repository CI without trusting artifacts or check names.

The webhook identifies a run; only fresh authenticated REST responses attest it.
Identity resolution is separate from success validation so a trusted publisher can
report BLOCK for failed CI, but must never publish for a superseded event.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from sidq.bot.action import ActionError, GitHubClient

REQUIRED_STEPS = (
    "Check out repository",
    "Verify tested head",
    "Fetch sealed demo branches",
    "Set up Python 3.12",
    "Install project and development dependencies",
    "Check installed dependencies",
    "Ruff lint",
    "Ruff format",
    "Mypy",
    "Pytest",
)
_MAX_PAGES = 10
_PAGE_SIZE = 100
_DEFAULT_WORKFLOW_PATH = ".github/workflows/ci.yml"


class StaleRun(ActionError):
    """A superseded or ineligible event for which nothing must be published."""


@dataclass(frozen=True, slots=True)
class ValidationContext:
    number: int
    base_sha: str
    head_sha: str
    head_repository: str
    run_id: int
    run_attempt: int
    run_url: str


def resolve_context(
    client: GitHubClient,
    event: Mapping[str, Any],
    *,
    repository: str,
    expected_workflow_path: str = _DEFAULT_WORKFLOW_PATH,
) -> ValidationContext:
    """Resolve current trusted PR identity, including unsuccessful CI runs.

    A returned context does not attest success. Call ``validate_checks`` before
    publishing PASS. Call this again before publishing a failure or using a
    context prepared earlier; neither a webhook nor a saved context is fresh.
    """
    _repository(repository)
    event = _object(event, "workflow_run event")
    if event.get("action") != "completed":
        raise ActionError("expected a completed workflow_run event")
    _same_repository(event.get("repository"), repository, "event repository")
    trigger = _object(event.get("workflow_run"), "event workflow_run")
    if trigger.get("event") == "push":
        raise StaleRun("push CI is not pull-request validation evidence")
    if trigger.get("event") != "pull_request":
        raise ActionError("CI must have been triggered by pull_request")
    if trigger.get("status") != "completed":
        raise ActionError("event workflow_run is not completed")
    run_id = _positive_int(trigger.get("id"), "event run ID")
    attempt = _positive_int(trigger.get("run_attempt"), "event run attempt")
    context, run = _resolve_run(
        client, run_id, attempt, repository, expected_workflow_path
    )
    _same_repository(trigger.get("repository"), repository, "event run repository")
    _same_repository(
        trigger.get("head_repository"), context.head_repository, "event head repository"
    )
    if (
        _positive_int(trigger.get("workflow_id"), "event workflow ID")
        != run["workflow_id"]
    ):
        raise ActionError("event workflow ID does not match the authenticated run")
    _workflow_path(trigger.get("path"), expected_workflow_path)
    if _sha(trigger.get("head_sha"), "event head SHA") != context.head_sha:
        raise ActionError("event head SHA does not match the authenticated run")
    event_pull = _association(trigger)
    _check_association(event_pull, context, repository)
    for side, field in (("head", "head_repository"), ("base", "repository")):
        reference = _object(event_pull.get(side), f"event {side}")
        if "repo" in reference:
            event_repo = _object(reference["repo"], f"event {side} repository")
            trusted_repo = _object(run.get(field), f"authenticated {side} repository")
            if "id" in event_repo and _positive_int(
                event_repo["id"], "event repository ID"
            ) != _positive_int(trusted_repo.get("id"), "authenticated repository ID"):
                raise ActionError(
                    "event repository ID does not match the authenticated run"
                )
    return context


def validate_checks(
    client: GitHubClient,
    context: ValidationContext,
    *,
    repository: str,
    expected_workflow_path: str = _DEFAULT_WORKFLOW_PATH,
) -> None:
    """Require fresh successful CI and all mandatory steps for this attempt."""
    fresh, run = _resolve_run(
        client, context.run_id, context.run_attempt, repository, expected_workflow_path
    )
    if fresh != context:
        raise StaleRun("the prepared CI context is no longer current")
    _successful(run, "CI workflow run")
    prefix = _prefix(repository)
    jobs = _paginate(
        client,
        f"{prefix}/actions/runs/{context.run_id}/attempts/{context.run_attempt}/jobs",
        "jobs",
    )
    matches = [job for job in jobs if job.get("name") == "check"]
    if len(matches) != 1:
        raise ActionError("CI must contain exactly one required job named check")
    job = matches[0]
    if _positive_int(job.get("run_id"), "job run ID") != context.run_id:
        raise ActionError("required CI job belongs to another run")
    if _sha(job.get("head_sha"), "job head SHA") != context.head_sha:
        raise ActionError("required CI job belongs to another head SHA")
    # The attempt-specific endpoint is authoritative. Some API versions also
    # include run_attempt on jobs; never accept contradictory extra metadata.
    if (
        "run_attempt" in job
        and _positive_int(job["run_attempt"], "job run attempt") != context.run_attempt
    ):
        raise ActionError("required CI job belongs to another run attempt")
    _successful(job, "required CI job check")
    steps = _objects(job.get("steps"), "required CI job steps")
    for name in REQUIRED_STEPS:
        required = [step for step in steps if step.get("name") == name]
        if len(required) != 1:
            raise ActionError(f"CI must contain exactly one required step: {name}")
        _successful(required[0], f"required CI step {name}")
    # A rerun or synchronization may have begun while paginating jobs. This is
    # also needed when a later attempt reuses successful jobs from an old one.
    final, final_run = _resolve_run(
        client, context.run_id, context.run_attempt, repository, expected_workflow_path
    )
    if final != context:
        raise StaleRun("CI context changed while validating mandatory jobs")
    _successful(final_run, "CI workflow run")


def validate_run(
    client: GitHubClient,
    event: Mapping[str, Any],
    *,
    repository: str,
    expected_workflow_path: str = _DEFAULT_WORKFLOW_PATH,
) -> ValidationContext:
    """Resolve trusted PR identity and attest mandatory successful CI."""
    context = resolve_context(
        client,
        event,
        repository=repository,
        expected_workflow_path=expected_workflow_path,
    )
    validate_checks(
        client,
        context,
        repository=repository,
        expected_workflow_path=expected_workflow_path,
    )
    return context


def _resolve_run(
    client: GitHubClient,
    run_id: int,
    attempt: int,
    repository: str,
    workflow_path: str,
) -> tuple[ValidationContext, Mapping[str, Any]]:
    prefix = _prefix(repository)
    _positive_int(run_id, "run ID")
    _positive_int(attempt, "run attempt")
    if not isinstance(workflow_path, str) or not re.fullmatch(
        r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow_path
    ):
        raise ActionError("expected workflow path is invalid")
    run = _object(
        client._request("GET", f"{prefix}/actions/runs/{run_id}"), "workflow run"
    )
    if _positive_int(run.get("id"), "run ID") != run_id:
        raise ActionError("GitHub returned a different workflow run")
    latest_attempt = _positive_int(run.get("run_attempt"), "run attempt")
    if latest_attempt > attempt:
        raise StaleRun("a newer attempt superseded this CI event")
    if latest_attempt != attempt:
        raise ActionError("event run attempt is ahead of the authenticated run")
    run_status = run.get("status")
    if isinstance(run_status, str) and run_status in {
        "queued",
        "in_progress",
        "waiting",
        "pending",
        "requested",
    }:
        raise StaleRun("CI is no longer completed; refusing an obsolete event")
    if run.get("status") != "completed":
        raise ActionError("authenticated CI run status is invalid")
    _same_repository(run.get("repository"), repository, "run repository")
    workflow_id = _positive_int(run.get("workflow_id"), "workflow ID")
    _workflow_path(run.get("path"), workflow_path)
    if run.get("event") != "pull_request":
        raise ActionError("authenticated CI run is not a pull_request run")
    workflow = _object(
        client._request("GET", f"{prefix}/actions/workflows/{workflow_id}"), "workflow"
    )
    if (
        _positive_int(workflow.get("id"), "workflow ID") != workflow_id
        or workflow.get("path") != workflow_path
    ):
        raise ActionError("authenticated workflow does not match required CI")
    association = _association(run)
    number = _positive_int(association.get("number"), "run pull-request number")
    pull = _object(client._request("GET", f"{prefix}/pulls/{number}"), "pull request")
    if _positive_int(pull.get("number"), "pull-request number") != number:
        raise ActionError("GitHub returned a different pull request")
    if pull.get("state") == "closed":
        raise StaleRun("the CI pull request is no longer open")
    if pull.get("state") != "open":
        raise ActionError("pull-request state is invalid")
    head = _object(pull.get("head"), "pull-request head")
    base = _object(pull.get("base"), "pull-request base")
    head_repository = _repository_name(head.get("repo"), "pull-request head repository")
    _same_repository(base.get("repo"), repository, "pull-request base repository")
    _same_repository(run.get("head_repository"), head_repository, "run head repository")
    head_sha = _sha(head.get("sha"), "pull-request head SHA")
    if _sha(run.get("head_sha"), "run head SHA") != head_sha:
        raise StaleRun("the pull-request head changed after this CI run")
    context = ValidationContext(
        number=number,
        base_sha=_sha(base.get("sha"), "pull-request base SHA"),
        head_sha=head_sha,
        head_repository=head_repository,
        run_id=run_id,
        run_attempt=attempt,
        run_url=_run_url(run.get("html_url"), repository, run_id),
    )
    _check_association(association, context, repository)
    # Run PR associations carry abbreviated repositories on GitHub. Compare
    # immutable IDs as well when present, without assuming full_name exists.
    for side in ("head", "base"):
        short = _object(association.get(side), f"run pull-request {side}")
        current = _object(pull.get(side), f"pull-request {side}")
        if "repo" in short:
            short_repo = _object(short["repo"], f"run pull-request {side} repository")
            current_repo = _object(
                current.get("repo"), f"pull-request {side} repository"
            )
            if _positive_int(short_repo.get("id"), "associated repository ID") != (
                _positive_int(current_repo.get("id"), "current repository ID")
            ):
                raise ActionError("run pull-request repository ID does not match")
    _verify_base_ancestor(client, context, repository)
    _latest_run(client, run, context, repository, workflow_path)
    return context, run


def _verify_base_ancestor(
    client: GitHubClient, context: ValidationContext, repository: str
) -> None:
    """Bind tested immutable head to the current base without mutable PR data.

    GitHub's run.pull_requests entries can change after a run finishes. A head
    that contains the current base, tested by explicit-head checkout plus the
    required Verify tested head step, proves the exact applicable source tree.
    Diverged branches must merge/rebase the current base and run CI again.
    """
    comparison = _object(
        client._request(
            "GET",
            f"{_prefix(repository)}/compare/{context.base_sha}...{context.head_sha}",
        ),
        "base ancestry comparison",
    )
    base = _object(comparison.get("base_commit"), "compared base commit")
    if _sha(base.get("sha"), "compared base SHA") != context.base_sha:
        raise ActionError("GitHub comparison does not match the pinned base SHA")
    merge_base = _object(comparison.get("merge_base_commit"), "compared merge base")
    merge_base_sha = _sha(merge_base.get("sha"), "compared merge-base SHA")
    status = comparison.get("status")
    if not isinstance(status, str) or status not in {
        "ahead",
        "identical",
        "behind",
        "diverged",
    }:
        raise ActionError("GitHub comparison status is invalid")
    if status not in {"ahead", "identical"} or merge_base_sha != context.base_sha:
        raise StaleRun(
            "the tested head must contain the current base; update the branch and rerun CI"
        )
    if (status == "identical") != (context.base_sha == context.head_sha):
        raise ActionError("GitHub comparison status contradicts the pinned revisions")


def _latest_run(
    client: GitHubClient,
    run: Mapping[str, Any],
    context: ValidationContext,
    repository: str,
    workflow_path: str,
) -> None:
    workflow_id = _positive_int(run.get("workflow_id"), "workflow ID")
    run_number = _positive_int(run.get("run_number"), "run number")
    query = urlencode(
        {
            "event": "pull_request",
            "head_sha": context.head_sha,
            "exclude_pull_requests": "false",
        }
    )
    runs = _paginate(
        client,
        f"{_prefix(repository)}/actions/workflows/{workflow_id}/runs?{query}",
        "workflow_runs",
    )
    found = False
    for candidate in runs:
        # GitHub sometimes omits associations from list responses, even with
        # exclude_pull_requests=false. Hydrate by authenticated numeric run ID;
        # never infer a PR from a matching branch or SHA.
        if (
            candidate.get("pull_requests") is None
            or candidate.get("pull_requests") == []
        ):
            candidate_id = _positive_int(candidate.get("id"), "listed run ID")
            hydrated = _object(
                client._request(
                    "GET", f"{_prefix(repository)}/actions/runs/{candidate_id}"
                ),
                "listed workflow run",
            )
            if _positive_int(hydrated.get("id"), "hydrated run ID") != candidate_id:
                raise ActionError("GitHub hydrated a different workflow run")
            for field in ("workflow_id", "run_number", "event", "head_sha", "path"):
                if field in candidate and candidate[field] != hydrated.get(field):
                    raise ActionError("hydrated CI run contradicts its listing")
            candidate = hydrated
        _same_repository(
            candidate.get("repository"), repository, "listed run repository"
        )
        if (
            _positive_int(candidate.get("workflow_id"), "listed workflow ID")
            != workflow_id
            or candidate.get("event") != "pull_request"
            or _sha(candidate.get("head_sha"), "listed head SHA") != context.head_sha
        ):
            raise ActionError("GitHub returned a run outside the requested CI scope")
        _workflow_path(candidate.get("path"), workflow_path)
        associated = _association(candidate)
        if (
            _positive_int(associated.get("number"), "listed pull-request number")
            != context.number
        ):
            continue  # The same commit may legitimately belong to multiple PRs.
        _same_repository(
            candidate.get("head_repository"),
            context.head_repository,
            "listed head repository",
        )
        candidate_id = _positive_int(candidate.get("id"), "listed run ID")
        candidate_number = _positive_int(
            candidate.get("run_number"), "listed run number"
        )
        candidate_attempt = _positive_int(
            candidate.get("run_attempt"), "listed run attempt"
        )
        if candidate_number > run_number:
            raise StaleRun(
                "a newer CI run superseded this event for the same pull request"
            )
        if candidate_id == context.run_id:
            if candidate_attempt > context.run_attempt:
                raise StaleRun("a newer CI attempt superseded this event")
            if (
                candidate_attempt != context.run_attempt
                or candidate_number != run_number
            ):
                raise ActionError("CI run listing contradicts the authenticated run")
            _check_association(associated, context, repository)
            found = True
        elif candidate_number == run_number:
            raise ActionError("CI run ordering is ambiguous")
    if not found:
        raise ActionError(
            "current CI run was missing from the authenticated run listing"
        )


def _paginate(client: GitHubClient, path: str, field: str) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    total: int | None = None
    for page in range(1, _MAX_PAGES + 1):
        separator = "&" if "?" in path else "?"
        query = urlencode({"per_page": _PAGE_SIZE, "page": page})
        document = _object(client._request("GET", f"{path}{separator}{query}"), field)
        count = document.get("total_count")
        if type(count) is not int or count < 0:
            raise ActionError("GitHub returned an invalid pagination total")
        if count >= _MAX_PAGES * _PAGE_SIZE:
            # Filtered Actions searches can silently cap results at 1,000.
            raise ActionError(
                "GitHub pagination limit reached; refusing partial CI evidence"
            )
        if total is not None and count != total:
            raise ActionError("GitHub pagination changed while validating CI")
        total = count
        batch = _objects(document.get(field), field)
        if len(batch) > _PAGE_SIZE:
            raise ActionError("GitHub returned an oversized CI page")
        for item in batch:
            item_id = _positive_int(item.get("id"), f"{field} ID")
            if item_id in seen:
                raise ActionError("GitHub returned duplicate paginated CI evidence")
            seen.add(item_id)
        items.extend(batch)
        if len(items) > total:
            raise ActionError("GitHub pagination total contradicts CI evidence")
        if len(items) == total:
            return items
        if len(batch) < _PAGE_SIZE:
            raise ActionError("GitHub returned incomplete paginated CI evidence")
    raise ActionError("GitHub pagination limit reached; refusing partial CI evidence")


def _check_association(
    pull: Mapping[str, Any], context: ValidationContext, repository: str
) -> None:
    if (
        _positive_int(pull.get("number"), "associated pull-request number")
        != context.number
    ):
        raise ActionError("CI is associated with a different pull request")
    for side, expected_sha, expected_repo in (
        ("head", context.head_sha, context.head_repository),
        ("base", context.base_sha, repository),
    ):
        reference = _object(pull.get(side), f"associated {side}")
        if _sha(reference.get("sha"), f"associated {side} SHA") != expected_sha:
            raise StaleRun(f"the pull-request {side} changed after this CI run")
        if "repo" in reference:
            repo = _object(reference["repo"], f"associated {side} repository")
            if "full_name" in repo:
                _same_repository(repo, expected_repo, f"associated {side} repository")


def _association(run: Mapping[str, Any]) -> Mapping[str, Any]:
    pulls = _objects(run.get("pull_requests"), "run pull requests")
    if len(pulls) != 1:
        raise ActionError("CI must identify exactly one pull request")
    return pulls[0]


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ActionError(f"GitHub returned invalid {label}")
    return value


def _objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ActionError(f"GitHub returned invalid {label}")
    return [_object(item, label) for item in value]


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ActionError(f"GitHub returned an invalid {label}")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ActionError(f"GitHub returned an invalid {label}")
    return value.lower()


def _repository(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value
    ):
        raise ActionError("repository must have the form owner/repository")
    return value


def _repository_name(value: Any, label: str) -> str:
    return _repository(_object(value, label).get("full_name"))


def _same_repository(value: Any, expected: str, label: str) -> None:
    if _repository_name(value, label).casefold() != expected.casefold():
        raise ActionError(f"{label} does not match the expected repository")


def _prefix(repository: str) -> str:
    return "/repos/" + "/".join(
        quote(part, safe="") for part in _repository(repository).split("/")
    )


def _workflow_path(value: Any, expected: str) -> None:
    # GitHub may append @ref to a run path; workflow metadata has the bare path.
    if (
        not isinstance(value, str)
        or value.partition("@")[0] != expected
        or ("@" in value and not value.partition("@")[2])
        or any(ord(char) < 32 for char in value)
    ):
        raise ActionError("CI workflow path does not match the required workflow")


def _run_url(value: Any, repository: str, run_id: int) -> str:
    if not isinstance(value, str):
        raise ActionError("CI run URL is missing")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ActionError("CI run URL is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.casefold() != f"/{repository}/actions/runs/{run_id}".casefold()
        or any(ord(char) < 32 for char in value)
    ):
        raise ActionError("CI run URL is invalid")
    return value


def _successful(value: Mapping[str, Any], label: str) -> None:
    if value.get("status") != "completed" or value.get("conclusion") != "success":
        raise ActionError(f"{label} did not complete successfully")
