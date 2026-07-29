from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from sidq.bot import action
from sidq.bot.comment import STICKY_MARKER, render_comment
from sidq.models import Evidence, FieldRef, Finding, TouchedAsset, Verdict

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / "examples" / "01-blocked-pii-dashboard" / "verdict.json"
FIXTURES = ROOT / "tests" / "fixtures" / "graph"


def _example_verdict() -> Verdict:
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return Verdict(
        decision=raw["decision"],
        reason_code=raw["reason_code"],
        findings=tuple(
            Finding(
                rule_id=finding["rule_id"],
                severity=finding["severity"],
                message=finding["message"],
                evidence=tuple(
                    Evidence(
                        kind=evidence["kind"],
                        subject=evidence["subject"],
                        detail=evidence["detail"],
                        graph_links=tuple(evidence.get("graph_links", ())),
                    )
                    for evidence in finding["evidence"]
                ),
            )
            for finding in raw["findings"]
        ),
        touched=tuple(
            TouchedAsset(
                urn=item["urn"],
                source_path=item["source_path"],
                added_fields=tuple(item["added_fields"]),
                removed_fields=tuple(item["removed_fields"]),
                referenced_fields=tuple(
                    FieldRef(**reference) for reference in item["referenced_fields"]
                ),
                resolution_strategy=item["resolution_strategy"],
            )
            for item in raw["touched"]
        ),
        commit_sha=raw["commit_sha"],
        policy_hash=raw["policy_hash"],
    )


def test_real_block_comment_is_deterministic_and_decision_first() -> None:
    verdict = _example_verdict()

    first = render_comment(verdict)
    second = render_comment(verdict)

    assert first.encode() == second.encode()
    assert first.startswith(
        f"{STICKY_MARKER}\n"
        "# 🚫 BLOCKED — <code>pii_exposure</code>, "
        "<code>critical_downstream</code>\n"
    )
    assert "**Why:** PII exposure is not permitted" in first
    # Host-agnostic: assert every graph link the verdict carries is actually rendered,
    # rather than pinning one hostname. Published artifacts point at the public DataHub
    # (SIDQ_DATAHUB_UI_URL); local runs default to localhost. Both must render links.
    rendered_links = {
        link
        for finding in verdict.findings
        for evidence in finding.evidence
        for link in evidence.graph_links
    }
    assert rendered_links, "the example verdict must carry graph links"
    assert all(link in first for link in rendered_links)
    assert "Column-level impact path:" in first
    assert "order_entry_db.order_entry.customers.cust_email" in first
    assert "Looker dashboard · dashboards.53" in first
    assert "<summary>Downstream consumers (16)</summary>" in first
    assert "urn:li:dataset:" not in first
    assert f"policy_hash={verdict.policy_hash}" in first
    assert f"commit_sha={verdict.commit_sha}" in first
    assert (
        f"sidq check --diff {verdict.commit_sha}^..{verdict.commit_sha} --json" in first
    )
    saved = EXAMPLE.with_name("pr-comment.md").read_text(encoding="utf-8")
    assert first == saved
    docs = (ROOT / "docs" / "PR-BOT.md").read_text(encoding="utf-8")
    assert docs[docs.index(STICKY_MARKER) :] == saved


def test_fixture_provenance_cannot_be_mistaken_for_live() -> None:
    rendered = render_comment(_example_verdict(), mode="fixture")

    assert "**FIXTURE REPLAY — NOT LIVE DATAHUB.**" in rendered
    assert "sealed demo pull requests for live-graph verdicts" in rendered
    assert "Provenance: LIVE DATAHUB" not in rendered


def test_advisory_model_evidence_is_visibly_non_blocking() -> None:
    deterministic = Finding(
        "unowned_asset",
        "warn",
        "The touched asset has no owner.",
        (Evidence("unowned_asset", "warehouse.customers", {}),),
    )
    advisory = Finding(
        "semantic_drift",
        "advisory",
        "The description may not match downstream use.",
        (
            Evidence(
                "semantic_drift",
                "customers.region",
                {"advisory": True, "model": "all-MiniLM"},
            ),
        ),
    )
    verdict = Verdict("WARN", None, (deterministic, advisory), (), "a" * 40, "p")

    rendered = render_comment(verdict)

    assert rendered.startswith(
        f"{STICKY_MARKER}\n# ⚠️ WARN — <code>unowned_asset</code>"
    )
    assert "## Non-blocking model-assisted evidence" in rendered
    assert "**Advisory only.**" in rendered
    assert "<code>all-MiniLM</code>" in rendered


def test_advisory_only_warn_names_the_non_blocking_rule() -> None:
    advisory = Finding(
        "semantic_drift",
        "advisory",
        "The description may not match downstream use.",
        (
            Evidence(
                "semantic_drift",
                "customers.region",
                {"advisory": True, "model": "all-MiniLM"},
            ),
        ),
    )
    verdict = Verdict("WARN", None, (advisory,), (), "a" * 40, "policy")

    rendered = render_comment(verdict)

    assert rendered.startswith(
        f"{STICKY_MARKER}\n"
        "# ⚠️ WARN — <code>semantic_drift</code> (non-blocking advisory)"
    )
    assert "unclassified_policy_failure" not in rendered


def test_pass_headline_is_an_explicit_policy_decision() -> None:
    verdict = Verdict("PASS", None, (), (), "a" * 40, "policy")

    assert "# ✅ PASS — no blocking or warning policy rule fired" in render_comment(
        verdict
    )


class _RecordingGitHubClient(action.GitHubClient):
    def __init__(self, comments: list[dict[str, Any]]) -> None:
        super().__init__("owner/repo", "token")
        self.comments = comments
        self.calls: list[tuple[str, str, Any]] = []

    def _paginate(self, path: str, *, maximum_pages: int = 100) -> list[dict[str, Any]]:
        self.calls.append(("PAGINATE", path, maximum_pages))
        return self.comments

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, body))
        return {}


def test_sticky_comment_is_updated_instead_of_duplicated() -> None:
    client = _RecordingGitHubClient(
        [
            {
                "id": 42,
                "body": f"{STICKY_MARKER}\nold",
                "user": {"type": "Bot"},
            }
        ]
    )

    client.upsert_comment(7, f"{STICKY_MARKER}\nnew")

    assert client.calls[-1] == (
        "PATCH",
        "/repos/owner/repo/issues/comments/42",
        {"body": f"{STICKY_MARKER}\nnew"},
    )


def test_human_marker_spoof_is_not_edited() -> None:
    client = _RecordingGitHubClient(
        [
            {
                "id": 42,
                "body": f"{STICKY_MARKER}\nspoof",
                "user": {"type": "User"},
            }
        ]
    )

    client.upsert_comment(7, f"{STICKY_MARKER}\nnew")

    assert client.calls[-1][0:2] == (
        "POST",
        "/repos/owner/repo/issues/7/comments",
    )


@pytest.mark.parametrize(
    ("exit_code", "conclusion"),
    [(0, "success"), (1, "neutral"), (2, "failure")],
)
def test_check_conclusion_is_derived_from_exit_code(
    exit_code: int, conclusion: str
) -> None:
    client = _RecordingGitHubClient([])

    client.create_check(
        head_sha="a" * 40,
        exit_code=exit_code,
        decision={0: "PASS", 1: "WARN", 2: "BLOCK"}[exit_code],
        mode="fixture",
    )

    method, path, body = client.calls[-1]
    assert (method, path) == ("POST", "/repos/owner/repo/check-runs")
    assert body["conclusion"] == conclusion
    assert body["head_sha"] == "a" * 40
    assert "recorded graph fixtures" in body["output"]["summary"]


def test_fixture_engine_runs_against_recorded_graph(tmp_path: Path) -> None:
    repo_root = tmp_path / "dbt"
    shutil.copytree(ROOT / "demo" / "dbt", repo_root)
    (repo_root / "models" / "order_entry" / "customers.sql").write_text(
        (ROOT / "examples" / "01-blocked-pii-dashboard" / "customers.sql").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    verdict = action.run_engine(
        ["models/order_entry/customers.sql"],
        repo_root=repo_root,
        commit_sha="a" * 40,
        mode="fixture",
        fixture_dir=FIXTURES,
    )

    assert verdict.decision == "BLOCK"
    assert "pii_exposure" in {finding.rule_id for finding in verdict.findings}
    assert "**FIXTURE REPLAY — NOT LIVE DATAHUB.**" in render_comment(
        verdict, mode="fixture"
    )


def test_pull_request_event_context_uses_immutable_shas(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "number": 9,
                "pull_request": {
                    "number": 9,
                    "base": {"sha": "a" * 40},
                    "head": {"sha": "b" * 40},
                },
            }
        ),
        encoding="utf-8",
    )

    assert action._context_from_event(event_path) == action.PullRequestContext(
        9, "a" * 40, "b" * 40
    )


@pytest.mark.parametrize("filename", ["../secret.sql", "/tmp/model.sql", ""])
def test_changed_file_paths_cannot_escape_the_checkout(filename: str) -> None:
    with pytest.raises(action.ActionError):
        action._safe_repo_path(filename)


def test_default_action_policy_comes_from_the_trusted_action_checkout() -> None:
    assert action._policy_path(ROOT, ROOT, "") == (
        ROOT / "src" / "sidq" / "policy" / "default_policy.yaml"
    )


def test_repository_policy_cannot_escape_repo_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(action.ActionError):
        action._policy_path(repo_root, ROOT, "../outside.yaml")


def test_graph_link_cannot_inject_a_markdown_line() -> None:
    finding = Finding(
        "unknown_field",
        "block",
        "A field is unknown.",
        (
            Evidence(
                "unknown_field",
                "warehouse.customers",
                {},
                ("https://datahub.example/dataset/value\n# injected",),
            ),
        ),
    )
    verdict = Verdict("BLOCK", None, (finding,), (), "a" * 40, "policy")

    rendered = render_comment(verdict)

    assert "\n# injected" not in rendered
    assert "%0A%23%20injected" in rendered


def test_comment_failure_does_not_prevent_check_publication() -> None:
    calls: list[str] = []

    class FailingCommentClient:
        def upsert_comment(self, pull_number: int, body: str) -> None:
            calls.append("comment")
            raise action.ActionError("comment denied")

        def create_check(
            self,
            *,
            head_sha: str,
            exit_code: int,
            decision: str,
            mode: str,
        ) -> None:
            calls.append("check")

    with pytest.raises(action.ActionError):
        action._publish_result(
            FailingCommentClient(),
            pull_number=7,
            head_sha="a" * 40,
            comment="decision",
            exit_code=2,
            decision="BLOCK",
            mode="fixture",
        )

    assert calls == ["comment", "check"]
