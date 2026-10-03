from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from sidq.bot import repository
from sidq.bot.action import ActionError
from sidq.bot.applicability import Applicability
from sidq.bot.validation import StaleRun, ValidationContext
from sidq.models import Verdict

ROOT = Path(__file__).parents[1]
CTX = ValidationContext(
    6,
    "a" * 40,
    "b" * 40,
    "NexuChat/sidq",
    123,
    1,
    "https://github.com/NexuChat/sidq/actions/runs/123",
)


def assessment(monkeypatch, kind="non_data"):
    client = Mock()
    monkeypatch.setattr(repository, "resolve_context", Mock(return_value=CTX))
    monkeypatch.setattr(repository, "validate_checks", Mock())
    monkeypatch.setattr(
        repository,
        "checkout_sha",
        lambda root: CTX.base_sha if root.name == "base" else CTX.head_sha,
    )
    monkeypatch.setattr(
        repository,
        "changed_records",
        Mock(return_value=([{"filename": "README.md", "status": "modified"}], 1)),
    )
    monkeypatch.setattr(
        repository,
        "classify_changes",
        Mock(
            return_value=Applicability(
                kind, ("documentation",), ("README.md",), "Known maintenance only."
            )
        ),
    )
    monkeypatch.setattr(
        repository,
        "run_engine",
        Mock(
            return_value=Verdict(
                "BLOCK", "unresolved_asset", (), (), CTX.head_sha, "policy"
            )
        ),
    )
    monkeypatch.setattr(repository, "validate_exports", Mock())
    publisher = Mock()
    monkeypatch.setattr(repository, "publish", publisher)
    return client, publisher


def test_non_data_is_neutral_without_engine_or_pass_certificate(monkeypatch):
    client, publisher = assessment(monkeypatch)
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 0
    )
    repository.run_engine.assert_not_called()
    body = publisher.call_args.kwargs
    assert body["conclusion"] == "neutral"
    assert "NOT APPLICABLE" in body["comment"]
    assert "no data-asset certification" in body["comment"]
    assert "PASS" not in body["comment"]
    assert "FIXTURE REPLAY" not in body["comment"]
    assert CTX.head_sha in body["comment"] and CTX.base_sha in body["comment"]
    assert repository.validate_checks.call_count == 2


def test_data_runs_unchanged_strict_engine_against_trusted_policy(monkeypatch):
    client, publisher = assessment(monkeypatch, "data")
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    repository.run_engine.assert_called_once()
    options = repository.run_engine.call_args.kwargs
    assert options["policy_path"] == Path("base/src/sidq/policy/default_policy.yaml")
    assert options["fixture_dir"] == Path("base/tests/fixtures/graph")
    assert publisher.call_args.kwargs["conclusion"] == "failure"
    assert "FIXTURE REPLAY" in publisher.call_args.kwargs["comment"]


def test_protected_or_mixed_diffs_block_without_fake_graph_evidence(monkeypatch):
    client, publisher = assessment(monkeypatch, "blocked")
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    repository.run_engine.assert_not_called()
    assert publisher.call_args.kwargs["conclusion"] == "failure"
    assert "No graph verdict" in publisher.call_args.kwargs["comment"]


def test_failed_mandatory_checks_block_before_classification(monkeypatch):
    client, publisher = assessment(monkeypatch)
    repository.validate_checks.side_effect = ActionError("tests failed")
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    repository.classify_changes.assert_not_called()
    assert publisher.call_args.kwargs["conclusion"] == "failure"
    assert repository.resolve_context.call_count == 2


@pytest.mark.parametrize("step", ["resolve_context", "validate_checks"])
def test_superseded_events_publish_nothing(monkeypatch, step):
    client, publisher = assessment(monkeypatch)
    getattr(repository, step).side_effect = StaleRun("newer run")
    with pytest.raises(StaleRun):
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
    publisher.assert_not_called()


def test_failure_racing_a_new_head_does_not_publish(monkeypatch):
    client, publisher = assessment(monkeypatch)
    repository.validate_checks.side_effect = ActionError("failed")
    repository.resolve_context.side_effect = [CTX, StaleRun("new head")]
    with pytest.raises(StaleRun):
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
    publisher.assert_not_called()


def test_new_attempt_after_engine_cannot_receive_old_success(monkeypatch):
    client, publisher = assessment(monkeypatch)
    repository.validate_checks.side_effect = [None, StaleRun("new attempt")]
    with pytest.raises(StaleRun):
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
    publisher.assert_not_called()


def test_checkout_mismatch_blocks(monkeypatch):
    client, publisher = assessment(monkeypatch)
    monkeypatch.setattr(repository, "checkout_sha", lambda root: "c" * 40)
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    assert publisher.call_args.kwargs["conclusion"] == "failure"


def test_unexpected_engine_error_refuses_certification(monkeypatch):
    client, publisher = assessment(monkeypatch, "data")
    repository.run_engine.side_effect = RuntimeError("private error data")
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    assert "private error data" not in publisher.call_args.kwargs["comment"]
    assert publisher.call_args.kwargs["conclusion"] == "failure"


def test_check_is_neutral_bound_to_head_and_base_and_comment_updates():
    client = Mock()
    client._request.side_effect = [{"total_count": 0, "check_runs": []}, {"id": 42}, {}]
    repository.publish(
        client, CTX, comment="N/A", conclusion="neutral", title="Not applicable"
    )
    method, path, body = client._request.call_args.args
    assert method == "PATCH" and path.endswith("/check-runs/42")
    pending = client._request.call_args_list[1].args[2]
    assert pending["head_sha"] == CTX.head_sha
    assert CTX.base_sha in pending["external_id"]
    assert pending["status"] == "in_progress"
    assert body["conclusion"] == "neutral"
    client.upsert_comment.assert_called_once_with(6, "N/A")


def test_repeated_publication_updates_only_our_exact_check():
    client = Mock()
    client._request.return_value = {
        "total_count": 1,
        "check_runs": [
            {
                "id": 42,
                "name": "Sidq policy verdict",
                "head_sha": CTX.head_sha,
                "external_id": f"sidq:{CTX.head_sha}:{CTX.base_sha}",
                "app": {"slug": "github-actions"},
            }
        ],
    }
    repository.publish(
        client, CTX, comment="N/A", conclusion="neutral", title="Not applicable"
    )
    assert client._request.call_args.args[:2] == (
        "PATCH",
        "/repos/NexuChat/sidq/check-runs/42",
    )


def test_partial_check_inventory_cannot_be_used_for_publication():
    client = Mock()
    client._request.return_value = {"total_count": 100, "check_runs": []}
    with pytest.raises(ActionError):
        repository.publish(
            client, CTX, comment="N/A", conclusion="neutral", title="Not applicable"
        )
    client.upsert_comment.assert_not_called()


def test_changed_files_use_immutable_comparison_and_current_head():
    client = Mock()
    pull = {
        "state": "open",
        "base": {"sha": CTX.base_sha},
        "head": {"sha": CTX.head_sha},
        "changed_files": 1,
    }
    diff = {
        "base_commit": {"sha": CTX.base_sha},
        "merge_base_commit": {"sha": CTX.base_sha},
        "status": "ahead",
        "files": [{"filename": "README.md", "status": "modified"}],
    }
    client._request.side_effect = [pull, diff]
    assert repository.changed_records(client, CTX) == (diff["files"], 1)
    assert client._request.call_args.args[1].endswith(
        f"/compare/{CTX.base_sha}...{CTX.head_sha}"
    )
    diff["files"] *= 300
    client._request.side_effect = [pull, diff]
    with pytest.raises(ActionError, match="truncated"):
        repository.changed_records(client, CTX)
    pull["head"]["sha"] = "c" * 40
    client._request.side_effect = [pull]
    with pytest.raises(StaleRun):
        repository.changed_records(client, CTX)


def test_prepare_emits_only_verified_checkout_inputs(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {}}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_REPOSITORY", "NexuChat/sidq")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(repository, "resolve_context", Mock(return_value=CTX))
    monkeypatch.setattr(repository, "checkout_sha", lambda root: CTX.base_sha)
    monkeypatch.setattr("sys.argv", ["repository", "--prepare"])
    assert repository.main() == 0
    assert (
        output.read_text()
        == f"ready=true\nhead_sha={CTX.head_sha}\nhead_repository=NexuChat/sidq\n"
    )
    repository.resolve_context.side_effect = StaleRun("new run")
    before = output.read_text()
    assert repository.main() == 0
    assert output.read_text() == before


def test_workflow_isolates_privileged_publisher_and_preserves_qa():
    workflow = yaml.safe_load((ROOT / ".github/workflows/sidq-demo.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_run"}
    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "write",
        "checks": "write",
    }
    steps = workflow["jobs"]["verdict"]["steps"]
    trusted = steps[0]
    assert "ref" not in trusted["with"]  # workflow_run's trusted default branch
    data = next(s for s in steps if s["name"] == "Check out pull request files as data")
    assert data["with"]["path"] == ".sidq-pr"
    assert data["with"]["persist-credentials"] is False
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "pip" in runs and "--require-hashes" in runs
    assert ".sidq-pr" not in runs
    assert all("actions/cache" not in s.get("uses", "") for s in steps)
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    names = {s["name"] for s in ci["jobs"]["check"]["steps"]}
    assert {
        "Check installed dependencies",
        "Ruff lint",
        "Ruff format",
        "Mypy",
        "Pytest",
    } <= names
    assert "Validate the pull request with the local composite action" not in names
    checkout = ci["jobs"]["check"]["steps"][0]
    assert (
        checkout["with"]["ref"]
        == "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    verify = next(
        s for s in ci["jobs"]["check"]["steps"] if s["name"] == "Verify tested head"
    )
    assert "git rev-parse HEAD" in verify["run"]


def test_comment_failure_never_completes_a_neutral_check():
    client = Mock()
    client._request.side_effect = [{"total_count": 0, "check_runs": []}, {"id": 42}]
    client.upsert_comment.side_effect = ActionError("comment publication failed")
    with pytest.raises(ActionError):
        repository.publish(
            client, CTX, comment="N/A", conclusion="neutral", title="Not applicable"
        )
    assert client._request.call_count == 2
    assert client._request.call_args.args[2]["status"] == "in_progress"
    assert "conclusion" not in client._request.call_args.args[2]


def test_dependency_scope_requires_trusted_export_validation(monkeypatch):
    client, publisher = assessment(monkeypatch)
    repository.classify_changes.return_value = Applicability(
        "non_data", ("dependencies",), ("uv.lock",), "Dependency maintenance."
    )
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 0
    )
    repository.validate_exports.assert_called_once_with(Path("head"))
    assert publisher.call_args.kwargs["conclusion"] == "neutral"


def test_stale_exports_block_even_when_editable_ci_tests_pass(monkeypatch):
    client, publisher = assessment(monkeypatch)
    repository.classify_changes.return_value = Applicability(
        "non_data", ("dependencies",), ("uv.lock",), "Dependency maintenance."
    )
    repository.validate_exports.side_effect = ActionError("stale export")
    assert (
        repository.assess(client, {}, base_root=Path("base"), head_root=Path("head"))
        == 2
    )
    assert publisher.call_args.kwargs["conclusion"] == "failure"
    repository.run_engine.assert_not_called()
