"""The repository gate uses authenticated CI evidence, never artifacts/check names."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from sidq.bot import validation
from sidq.bot.action import ActionError, GitHubClient
from sidq.bot.validation import (
    REQUIRED_STEPS,
    StaleRun,
    ValidationContext,
    resolve_context,
    validate_checks,
    validate_run,
)

REPOSITORY = "owner/repo"
HEAD_REPOSITORY = "contributor/repo"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
PREFIX = f"/repos/{REPOSITORY}"
WORKFLOW_PATH = ".github/workflows/ci.yml"


class FakeClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__(REPOSITORY, "unit-test-token")
        head_repo = {"id": 2, "full_name": HEAD_REPOSITORY}
        base_repo = {"id": 1, "full_name": REPOSITORY}
        association = {
            "number": 17,
            "head": {"sha": HEAD_SHA, "repo": {"id": 2}},
            "base": {"sha": BASE_SHA, "repo": {"id": 1}},
        }
        self.run: dict[str, Any] = {
            "id": 101,
            "run_number": 7,
            "run_attempt": 2,
            "workflow_id": 33,
            "name": "CI",
            "path": WORKFLOW_PATH,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "head_sha": HEAD_SHA,
            "repository": deepcopy(base_repo),
            "head_repository": deepcopy(head_repo),
            "pull_requests": [association],
            "html_url": f"https://github.com/{REPOSITORY}/actions/runs/101",
        }
        self.event = {
            "action": "completed",
            "repository": deepcopy(base_repo),
            "workflow_run": deepcopy(self.run),
        }
        self.pull: dict[str, Any] = {
            "number": 17,
            "state": "open",
            "head": {"sha": HEAD_SHA, "repo": deepcopy(head_repo)},
            "base": {"sha": BASE_SHA, "repo": deepcopy(base_repo)},
        }
        self.workflow: dict[str, Any] = {"id": 33, "path": WORKFLOW_PATH, "name": "CI"}
        self.jobs: list[dict[str, Any]] = [
            {
                "id": 900,
                "run_id": 101,
                "run_attempt": 2,
                "head_sha": HEAD_SHA,
                "name": "check",
                "status": "completed",
                "conclusion": "success",
                "steps": [
                    {
                        "name": name,
                        "number": number,
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for number, name in enumerate(REQUIRED_STEPS, start=1)
                ],
            }
        ]
        self.comparison: dict[str, Any] = {
            "status": "ahead",
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": BASE_SHA},
        }
        self.runs: list[dict[str, Any]] | None = None
        self.responses: dict[str, Any] = {}
        self.calls: list[str] = []
        self.after_jobs: dict[str, Any] | None = None

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Any:
        assert method == "GET", "attestation must never mutate GitHub"
        assert body is None
        self.calls.append(path)
        if path in self.responses:
            response = self.responses[path]
            if isinstance(response, Exception):
                raise response
            return deepcopy(response)
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path == f"{PREFIX}/actions/runs/101":
            return deepcopy(self.run)
        if parsed.path == f"{PREFIX}/actions/workflows/33":
            return deepcopy(self.workflow)
        if parsed.path == f"{PREFIX}/pulls/17":
            return deepcopy(self.pull)
        if parsed.path == f"{PREFIX}/compare/{BASE_SHA}...{HEAD_SHA}":
            return deepcopy(self.comparison)
        if parsed.path == f"{PREFIX}/actions/workflows/33/runs":
            assert query["event"] == ["pull_request"]
            assert query["head_sha"] == [HEAD_SHA]
            assert query["exclude_pull_requests"] == ["false"]
            return self._page(
                self.runs if self.runs is not None else [self.run],
                query,
                "workflow_runs",
            )
        if parsed.path == f"{PREFIX}/actions/runs/101/attempts/2/jobs":
            result = self._page(self.jobs, query, "jobs")
            if self.after_jobs is not None:
                self.run.update(self.after_jobs)
            return result
        raise AssertionError(f"unexpected API request: {path}")

    @staticmethod
    def _page(
        items: list[dict[str, Any]], query: dict[str, list[str]], field: str
    ) -> dict[str, Any]:
        assert query["per_page"] == ["100"]
        page = int(query["page"][0])
        return {
            "total_count": len(items),
            field: deepcopy(items[(page - 1) * 100 : page * 100]),
        }


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


def _validate(client: FakeClient) -> ValidationContext:
    return validate_run(client, client.event, repository=REPOSITORY)


def _runs_page(page: int = 1) -> str:
    return (
        f"{PREFIX}/actions/workflows/33/runs?event=pull_request&head_sha={HEAD_SHA}"
        f"&exclude_pull_requests=false&per_page=100&page={page}"
    )


def _jobs_page(page: int = 1) -> str:
    return f"{PREFIX}/actions/runs/101/attempts/2/jobs?per_page=100&page={page}"


def test_successful_attempt_is_authenticated_and_rechecked(client: FakeClient) -> None:
    context = _validate(client)
    assert context == ValidationContext(
        17, BASE_SHA, HEAD_SHA, HEAD_REPOSITORY, 101, 2, client.run["html_url"]
    )
    assert _jobs_page() in client.calls
    assert client.calls.count(f"{PREFIX}/actions/runs/101") == 3
    assert not any("check-runs" in call or "artifacts" in call for call in client.calls)


def test_identity_resolution_allows_failure_but_success_validation_blocks(
    client: FakeClient,
) -> None:
    client.run["conclusion"] = "failure"
    client.jobs[0]["conclusion"] = "failure"
    context = resolve_context(client, client.event, repository=REPOSITORY)
    assert context.head_sha == HEAD_SHA
    with pytest.raises(ActionError, match="workflow run did not complete successfully"):
        validate_checks(client, context, repository=REPOSITORY)


@pytest.mark.parametrize(
    "where", ["event", "trigger", "run", "base", "head", "trigger_head"]
)
def test_wrong_repository_is_rejected(client: FakeClient, where: str) -> None:
    repositories = {
        "event": client.event["repository"],
        "trigger": client.event["workflow_run"]["repository"],
        "run": client.run["repository"],
        "base": client.pull["base"]["repo"],
        "head": client.run["head_repository"],
        "trigger_head": client.event["workflow_run"]["head_repository"],
    }
    repositories[where]["full_name"] = "attacker/other"
    with pytest.raises(ActionError, match="repository"):
        _validate(client)


@pytest.mark.parametrize("where", ["metadata", "run", "event"])
def test_wrong_workflow_path_is_rejected(client: FakeClient, where: str) -> None:
    document = {
        "metadata": client.workflow,
        "run": client.run,
        "event": client.event["workflow_run"],
    }[where]
    document["path"] = ".github/workflows/attacker.yml"
    with pytest.raises(ActionError, match="workflow"):
        _validate(client)


@pytest.mark.parametrize("where", ["metadata", "event"])
def test_wrong_workflow_id_is_rejected(client: FakeClient, where: str) -> None:
    if where == "metadata":
        client.workflow["id"] = 45
    else:
        client.event["workflow_run"]["workflow_id"] = 45
    with pytest.raises(ActionError, match="workflow"):
        _validate(client)


def test_workflow_run_path_can_have_authenticated_ref_suffix(
    client: FakeClient,
) -> None:
    client.run["path"] += "@refs/pull/17/merge"
    client.event["workflow_run"]["path"] = client.run["path"]
    assert _validate(client).head_sha == HEAD_SHA


@pytest.mark.parametrize(
    "event", ["pull_request_target", "workflow_dispatch", "schedule", None]
)
def test_non_pull_request_event_is_rejected(client: FakeClient, event: Any) -> None:
    client.event["workflow_run"]["event"] = event
    with pytest.raises(ActionError, match="pull_request"):
        _validate(client)


def test_push_ci_is_ignored_without_publishing(client: FakeClient) -> None:
    client.event["workflow_run"]["event"] = "push"
    with pytest.raises(StaleRun):
        _validate(client)
    assert client.calls == []


def test_authenticated_event_cannot_differ_from_webhook(client: FakeClient) -> None:
    client.run["event"] = "push"
    with pytest.raises(ActionError, match="pull_request"):
        _validate(client)


@pytest.mark.parametrize(
    "key,value", [("action", "requested"), ("workflow_run", None), ("repository", None)]
)
def test_malformed_event_is_rejected(client: FakeClient, key: str, value: Any) -> None:
    client.event[key] = value
    with pytest.raises(ActionError):
        _validate(client)


@pytest.mark.parametrize(
    "key,value",
    [
        ("id", True),
        ("id", -1),
        ("id", "101"),
        ("run_attempt", 0),
        ("run_attempt", False),
        ("status", "queued"),
    ],
)
def test_invalid_event_fields_are_rejected(
    client: FakeClient, key: str, value: Any
) -> None:
    client.event["workflow_run"][key] = value
    with pytest.raises(ActionError):
        _validate(client)


@pytest.mark.parametrize("where", ["event", "run"])
@pytest.mark.parametrize("count", [0, 2])
def test_pull_request_association_must_be_unambiguous(
    client: FakeClient, where: str, count: int
) -> None:
    run = client.event["workflow_run"] if where == "event" else client.run
    run["pull_requests"] = run["pull_requests"] * count
    with pytest.raises(ActionError, match="exactly one pull request"):
        _validate(client)


@pytest.mark.parametrize("side", ["head", "base"])
def test_changed_pull_request_revision_is_stale(client: FakeClient, side: str) -> None:
    client.pull[side]["sha"] = "c" * 40
    with pytest.raises(StaleRun, match=f"{side} changed"):
        _validate(client)


@pytest.mark.parametrize("side", ["head", "base"])
def test_associated_repository_id_is_verified(client: FakeClient, side: str) -> None:
    client.run["pull_requests"][0][side]["repo"]["id"] = 77
    with pytest.raises(ActionError, match="repository ID"):
        _validate(client)


def test_wrong_pull_request_response_is_rejected(client: FakeClient) -> None:
    client.pull["number"] = 99
    with pytest.raises(ActionError, match="different pull request"):
        _validate(client)


def test_wrong_event_pull_request_is_rejected(client: FakeClient) -> None:
    client.event["workflow_run"]["pull_requests"][0]["number"] = 99
    with pytest.raises(ActionError, match="different pull request"):
        _validate(client)


def test_closed_pull_request_is_stale(client: FakeClient) -> None:
    client.pull["state"] = "closed"
    with pytest.raises(StaleRun, match="no longer open"):
        _validate(client)


@pytest.mark.parametrize("where", ["run", "job"])
@pytest.mark.parametrize(
    "conclusion", ["failure", "skipped", "neutral", "cancelled", "timed_out", None]
)
def test_unsuccessful_ci_cannot_attest_success(
    client: FakeClient, where: str, conclusion: Any
) -> None:
    (client.run if where == "run" else client.jobs[0])["conclusion"] = conclusion
    with pytest.raises(ActionError, match="did not complete successfully"):
        _validate(client)


@pytest.mark.parametrize("name", REQUIRED_STEPS)
@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "failed", "skipped", "in_progress"]
)
def test_every_required_step_must_uniquely_succeed(
    client: FakeClient, name: str, change: str
) -> None:
    steps = client.jobs[0]["steps"]
    step = next(item for item in steps if item["name"] == name)
    if change == "missing":
        steps.remove(step)
    elif change == "duplicate":
        steps.append(deepcopy(step))
    elif change == "in_progress":
        step["status"] = "in_progress"
    else:
        step["conclusion"] = "failure" if change == "failed" else "skipped"
    with pytest.raises(ActionError, match="required step|CI step"):
        _validate(client)


@pytest.mark.parametrize("change", ["missing", "duplicate", "renamed"])
def test_required_job_is_unique_and_named_check(
    client: FakeClient, change: str
) -> None:
    if change == "missing":
        client.jobs = []
    elif change == "duplicate":
        client.jobs.append({**deepcopy(client.jobs[0]), "id": 901})
    else:
        client.jobs[0]["name"] = "not-check"
    with pytest.raises(ActionError, match="exactly one required job"):
        _validate(client)


@pytest.mark.parametrize(
    "key,value", [("run_id", 202), ("run_attempt", 1), ("head_sha", "c" * 40)]
)
def test_required_job_must_match_exact_run_attempt_and_head(
    client: FakeClient, key: str, value: Any
) -> None:
    client.jobs[0][key] = value
    with pytest.raises(ActionError, match="another"):
        _validate(client)


def test_attempt_endpoint_is_authoritative_when_job_attempt_field_is_absent(
    client: FakeClient,
) -> None:
    del client.jobs[0]["run_attempt"]
    assert _validate(client).run_attempt == 2


@pytest.mark.parametrize("attempt", [3, 9])
def test_completed_event_for_older_attempt_is_stale(
    client: FakeClient, attempt: int
) -> None:
    client.run["run_attempt"] = attempt
    client.run["status"] = "queued"
    with pytest.raises(StaleRun, match="newer attempt"):
        _validate(client)
    assert _jobs_page() not in client.calls


def test_event_for_nonexistent_future_attempt_fails_closed(client: FakeClient) -> None:
    client.event["workflow_run"]["run_attempt"] = 3
    with pytest.raises(ActionError, match="ahead"):
        _validate(client)


def test_newer_ci_run_for_same_pr_supersedes_event_even_when_queued(
    client: FakeClient,
) -> None:
    newer = {
        **deepcopy(client.run),
        "id": 102,
        "run_number": 8,
        "status": "queued",
        "conclusion": None,
    }
    client.runs = [newer, client.run]
    with pytest.raises(StaleRun, match="newer CI run"):
        _validate(client)


def test_newer_run_for_other_pr_on_same_head_is_not_a_substitute(
    client: FakeClient,
) -> None:
    unrelated = {**deepcopy(client.run), "id": 102, "run_number": 8}
    unrelated["pull_requests"][0]["number"] = 18
    client.runs = [unrelated, client.run]
    assert _validate(client).number == 17
    client.runs = [unrelated]
    with pytest.raises(ActionError, match="missing"):
        _validate(client)


def test_newer_attempt_in_listing_is_stale(client: FakeClient) -> None:
    client.runs = [{**deepcopy(client.run), "run_attempt": 3}]
    with pytest.raises(StaleRun, match="newer CI attempt"):
        _validate(client)


@pytest.mark.parametrize("after", [{"run_attempt": 3}, {"conclusion": "failure"}])
def test_ci_is_rechecked_after_reading_jobs(
    client: FakeClient, after: dict[str, Any]
) -> None:
    client.after_jobs = after
    with pytest.raises(ActionError):
        _validate(client)


def test_prepared_context_cannot_be_reused_for_a_different_head(
    client: FakeClient,
) -> None:
    context = resolve_context(client, client.event, repository=REPOSITORY)
    with pytest.raises(StaleRun, match="no longer current"):
        validate_checks(
            client, replace(context, head_sha="c" * 40), repository=REPOSITORY
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        f"{PREFIX}/actions/runs/101",
        f"{PREFIX}/actions/workflows/33",
        f"{PREFIX}/pulls/17",
        _runs_page(),
        _jobs_page(),
    ],
)
def test_api_errors_fail_closed(client: FakeClient, endpoint: str) -> None:
    client.responses[endpoint] = ActionError("GitHub API unavailable")
    with pytest.raises(ActionError, match="unavailable"):
        _validate(client)


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"total_count": True, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": [None]},
        {"total_count": 2, "workflow_runs": []},
    ],
)
def test_malformed_pagination_fails_closed(client: FakeClient, response: Any) -> None:
    client.responses[_runs_page()] = response
    with pytest.raises(ActionError):
        _validate(client)


@pytest.mark.parametrize("count", [1000, 1001])
def test_github_search_truncation_limit_fails_closed(
    client: FakeClient, count: int
) -> None:
    client.responses[_runs_page()] = {
        "total_count": count,
        "workflow_runs": [client.run],
    }
    with pytest.raises(ActionError, match="pagination limit"):
        _validate(client)


def test_duplicate_paginated_evidence_fails_closed(client: FakeClient) -> None:
    client.runs = [client.run, client.run]
    with pytest.raises(ActionError, match="duplicate"):
        _validate(client)


def test_both_ci_runs_and_jobs_are_paginated(client: FakeClient) -> None:
    # Different PRs can share this head; every page must be inspected.
    client.runs = []
    for number in range(120):
        run = {**deepcopy(client.run), "id": 1000 + number, "run_number": 1000 + number}
        run["pull_requests"][0]["number"] = 2000 + number
        client.runs.append(run)
    client.runs.append(client.run)
    client.jobs = [
        {"id": 1000 + number, "name": f"unrelated-{number}"} for number in range(120)
    ] + client.jobs
    assert _validate(client).run_id == 101
    assert _runs_page(2) in client.calls
    assert _jobs_page(2) in client.calls


def test_changed_pagination_total_fails_closed(client: FakeClient) -> None:
    first = [{**deepcopy(client.run), "id": 1000 + index} for index in range(100)]
    client.responses[_runs_page()] = {"total_count": 101, "workflow_runs": first}
    client.responses[_runs_page(2)] = {
        "total_count": 102,
        "workflow_runs": [client.run],
    }
    with pytest.raises(ActionError, match="pagination changed"):
        _validate(client)


def test_bounded_pagination_fails_closed(
    client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(validation, "_MAX_PAGES", 1)
    client.responses[_runs_page()] = {"total_count": 100, "workflow_runs": [client.run]}
    with pytest.raises(ActionError, match="pagination limit"):
        _validate(client)


@pytest.mark.parametrize(
    "repository",
    ["owner/repo/other", "../repo", "owner/repo?token=x", "owner/", "owner/\nrepo"],
)
def test_repository_input_cannot_inject_api_paths(
    client: FakeClient, repository: str
) -> None:
    with pytest.raises(ActionError, match="repository"):
        validate_run(client, client.event, repository=repository)
    assert client.calls == []


@pytest.mark.parametrize("sha", ["b" * 39, "b" * 41, "z" * 40, None, 1])
def test_sha_must_be_full_git_object_id(client: FakeClient, sha: Any) -> None:
    client.run["head_sha"] = sha
    with pytest.raises(ActionError, match="SHA"):
        _validate(client)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo/actions/runs/101",
        "https://github.com/owner/repo/actions/runs/202",
        "https://user:secret@github.com/owner/repo/actions/runs/101",
        "https://github.com/owner/repo/actions/runs/101?data=secret",
        None,
    ],
)
def test_run_url_is_validated_before_rendering(client: FakeClient, url: Any) -> None:
    client.run["html_url"] = url
    with pytest.raises(ActionError, match="URL"):
        _validate(client)


@pytest.mark.parametrize("event", [None, [], "workflow_run"])
def test_top_level_event_must_be_object(client: FakeClient, event: Any) -> None:
    with pytest.raises(ActionError, match="workflow_run event"):
        validate_run(client, event, repository=REPOSITORY)


@pytest.mark.parametrize("side", ["head", "base"])
def test_event_association_repository_id_must_match_api(
    client: FakeClient, side: str
) -> None:
    client.event["workflow_run"]["pull_requests"][0][side]["repo"]["id"] = 999
    with pytest.raises(ActionError, match="event repository ID"):
        _validate(client)


@pytest.mark.parametrize(
    "status", ["queued", "in_progress", "waiting", "pending", "requested"]
)
def test_incomplete_fresh_run_suppresses_earlier_completed_event(
    client: FakeClient, status: str
) -> None:
    client.run["status"] = status
    with pytest.raises(StaleRun, match="no longer completed"):
        resolve_context(client, client.event, repository=REPOSITORY)


def test_invalid_fresh_status_fails_closed(client: FakeClient) -> None:
    client.run["status"] = None
    with pytest.raises(ActionError, match="status is invalid"):
        _validate(client)


def test_malformed_url_raises_action_error(client: FakeClient) -> None:
    client.run["html_url"] = "https://[malformed"
    with pytest.raises(ActionError, match="URL is invalid"):
        _validate(client)


@pytest.mark.parametrize("association", [None, []])
def test_missing_list_association_is_hydrated_from_authenticated_run(
    client: FakeClient, association: Any
) -> None:
    client.runs = [{**deepcopy(client.run), "pull_requests": association}]
    assert _validate(client).number == 17
    assert client.calls.count(f"{PREFIX}/actions/runs/101") == 6


def test_absent_list_association_field_is_hydrated(client: FakeClient) -> None:
    listed = deepcopy(client.run)
    del listed["pull_requests"]
    client.runs = [listed]
    assert _validate(client).number == 17


def test_hydrated_newer_run_for_same_pr_supersedes_event(client: FakeClient) -> None:
    newer = {**deepcopy(client.run), "id": 102, "run_number": 8, "status": "queued"}
    client.runs = [{**newer, "pull_requests": []}, client.run]
    client.responses[f"{PREFIX}/actions/runs/102"] = newer
    with pytest.raises(StaleRun, match="newer CI run"):
        _validate(client)


def test_hydrated_run_for_other_pr_is_not_misattributed(client: FakeClient) -> None:
    unrelated = {**deepcopy(client.run), "id": 102, "run_number": 8}
    unrelated["pull_requests"][0]["number"] = 18
    client.runs = [{**unrelated, "pull_requests": []}, client.run]
    client.responses[f"{PREFIX}/actions/runs/102"] = unrelated
    assert _validate(client).number == 17


@pytest.mark.parametrize("association", [None, [], [{"number": 17}, {"number": 18}]])
def test_hydration_cannot_guess_missing_or_ambiguous_association(
    client: FakeClient, association: Any
) -> None:
    incomplete = {**deepcopy(client.run), "id": 102, "run_number": 8}
    client.runs = [{**incomplete, "pull_requests": []}, client.run]
    incomplete["pull_requests"] = association
    client.responses[f"{PREFIX}/actions/runs/102"] = incomplete
    with pytest.raises(ActionError, match="pull requests|exactly one pull request"):
        _validate(client)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", 103),
        ("workflow_id", 44),
        ("head_sha", "c" * 40),
        ("event", "push"),
        ("run_number", 9),
        ("path", ".github/workflows/other.yml"),
    ],
)
def test_hydration_must_match_listed_identity(
    client: FakeClient, field: str, value: Any
) -> None:
    newer = {**deepcopy(client.run), "id": 102, "run_number": 8}
    client.runs = [{**newer, "pull_requests": []}, client.run]
    client.responses[f"{PREFIX}/actions/runs/102"] = {**newer, field: value}
    with pytest.raises(
        ActionError, match="different workflow run|contradicts its listing"
    ):
        _validate(client)


def test_hydration_error_fails_closed(client: FakeClient) -> None:
    newer = {**deepcopy(client.run), "id": 102, "run_number": 8, "pull_requests": []}
    client.runs = [newer, client.run]
    client.responses[f"{PREFIX}/actions/runs/102"] = ActionError("API unavailable")
    with pytest.raises(ActionError, match="API unavailable"):
        _validate(client)


def test_current_base_ancestry_is_proved_by_pinned_authenticated_compare(
    client: FakeClient,
) -> None:
    _validate(client)
    assert client.calls.count(f"{PREFIX}/compare/{BASE_SHA}...{HEAD_SHA}") == 3


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_head_must_contain_current_base(client: FakeClient, status: str) -> None:
    client.comparison["status"] = status
    client.comparison["merge_base_commit"]["sha"] = "c" * 40
    with pytest.raises(StaleRun, match="must contain the current base"):
        _validate(client)


def test_mutable_run_base_association_cannot_substitute_for_ancestry_proof(
    client: FakeClient,
) -> None:
    # Every mutable PR field claims the current base, but the immutable head
    # still descends from an older commit. No result may be published.
    client.comparison["merge_base_commit"]["sha"] = "c" * 40
    with pytest.raises(StaleRun, match="must contain the current base"):
        resolve_context(client, client.event, repository=REPOSITORY)


@pytest.mark.parametrize(
    "comparison",
    [
        None,
        [],
        {},
        {"status": "ahead", "base_commit": None},
        {
            "status": "ahead",
            "base_commit": {"sha": BASE_SHA},
            "merge_base_commit": {"sha": "invalid"},
        },
    ],
)
def test_malformed_compare_fails_closed(client: FakeClient, comparison: Any) -> None:
    client.responses[f"{PREFIX}/compare/{BASE_SHA}...{HEAD_SHA}"] = comparison
    with pytest.raises(ActionError):
        _validate(client)


def test_compare_must_confirm_exact_pinned_base(client: FakeClient) -> None:
    client.comparison["base_commit"]["sha"] = "c" * 40
    with pytest.raises(ActionError, match="pinned base SHA"):
        _validate(client)


@pytest.mark.parametrize("status", [None, "unknown", "identical", [], {}])
def test_compare_status_must_be_valid_and_consistent(
    client: FakeClient, status: Any
) -> None:
    client.comparison["status"] = status
    with pytest.raises(ActionError, match="comparison status"):
        _validate(client)


def test_compare_api_failure_cannot_produce_attestation(client: FakeClient) -> None:
    client.responses[f"{PREFIX}/compare/{BASE_SHA}...{HEAD_SHA}"] = ActionError(
        "compare unavailable"
    )
    with pytest.raises(ActionError, match="compare unavailable"):
        _validate(client)


def test_pre_explicit_head_verification_workflow_cannot_attest(
    client: FakeClient,
) -> None:
    assert "Verify tested head" in REQUIRED_STEPS
    client.jobs[0]["steps"] = [
        step for step in client.jobs[0]["steps"] if step["name"] != "Verify tested head"
    ]
    with pytest.raises(ActionError, match="required step: Verify tested head"):
        _validate(client)
