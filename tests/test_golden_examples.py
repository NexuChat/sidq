"""Golden end-to-end regression over the published examples.

`docs/ENGINE-SPEC.md` §7 requires a golden test that pins the flagship example's
verdict: the engine must keep producing the exact decision and rule ids that the
repository publishes. It named fixtures (`examples/bad_change.sql`,
`examples/good_change.sql`) that were never created, so the regression it
specifies did not exist and the engine could have silently changed its verdict on
the artifact a judge actually opens.

This closes that gap against the real published example instead of inventing new
fixtures, and it runs entirely offline from the committed graph replay snapshot.
Never edit `verdict.json` to make this pass — fix the engine.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import regenerate_example_01
from sidq import cli
from sidq.graph.fixtures import ReplayGraphClient
from sidq.policy.engine import default_policy_path
from sidq.serialization import canonical_json

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "graph"
BLOCKED = ROOT / "examples" / "01-blocked-pii-dashboard"


def _published() -> dict:
    return json.loads((BLOCKED / "verdict.json").read_text(encoding="utf-8"))


def _decide(sql_path: Path, project_root: Path) -> dict:
    """Run the real engine over one SQL file with a replayed graph, no network."""
    verdict = cli.check(
        [str(sql_path)],
        graph=ReplayGraphClient(FIXTURES),
        live_source=None,
        repo_root=project_root,
        commit_sha=_published()["commit_sha"],
    )
    return json.loads(canonical_json(verdict))


def _project(tmp_path: Path, sql: str) -> tuple[Path, Path]:
    """Recreate the example's dbt project shape so the resolver maps it to the URN.

    The manifest must declare the removed column too. The published BLOCK depends
    on ``cust_email`` being in the catalog contract and absent from the SQL; a
    manifest listing only the surviving columns removes nothing and passes.
    """
    published = _published()
    touched = published["touched"][0]
    urn = touched["urn"]
    relation = urn.split(",")[1]
    columns = [*touched["added_fields"], *touched.get("removed_fields", ())]
    manifest = {
        "metadata": {"adapter_type": "dbt"},
        "nodes": {
            "model.sidq.customers": {
                "original_file_path": "models/customers.sql",
                "relation_name": relation,
                "config": {"meta": {"environment": "PROD"}},
                "columns": {column: {} for column in columns},
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    model = tmp_path / "models" / "customers.sql"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text(sql, encoding="utf-8")
    return model, tmp_path


def test_the_published_verdict_json_is_still_a_block() -> None:
    """The artifact a judge opens must not have drifted from the engine."""
    published = _published()

    assert published["decision"] == "BLOCK"
    assert published["policy_hash"]
    assert [finding["rule_id"] for finding in published["findings"]] == [
        "pii_exposure",
        "wide_blast_radius",
        "critical_downstream",
    ]


def test_the_blocked_example_still_blocks_with_the_same_rule_ids(
    tmp_path: Path,
) -> None:
    """ENGINE-SPEC §7: bad change ⇒ BLOCK with the exact published rule ids.

    Fully offline. The twelve downstream consumers this radius reaches were
    recorded by `scripts/record_missing_graph_fixtures.py`, so the ownership
    behind `critical_downstream` is now reproducible from the committed snapshot
    instead of requiring the live graph.
    """
    published = _published()
    sql = (BLOCKED / "customers.sql").read_text(encoding="utf-8")
    model, project_root = _project(tmp_path, sql)

    result = _decide(model, project_root)

    assert result["decision"] == published["decision"]
    assert [finding["rule_id"] for finding in result["findings"]] == [
        finding["rule_id"] for finding in published["findings"]
    ]


def test_the_cross_team_ownership_behind_the_block_is_reproduced(
    tmp_path: Path,
) -> None:
    """`critical_downstream` blocks on ownership, so ownership must be real."""
    published = _published()
    published_owners = next(
        evidence["detail"]["cross_team_owners"]
        for finding in published["findings"]
        if finding["rule_id"] == "critical_downstream"
        for evidence in finding["evidence"]
        if "cross_team_owners" in evidence["detail"]
    )
    sql = (BLOCKED / "customers.sql").read_text(encoding="utf-8")
    model, project_root = _project(tmp_path, sql)

    result = _decide(model, project_root)

    owners = next(
        evidence["detail"]["cross_team_owners"]
        for finding in result["findings"]
        for evidence in finding["evidence"]
        if evidence["kind"] == "blast_radius"
    )
    assert owners == published_owners


def test_the_column_lineage_behind_the_block_is_still_proven(tmp_path: Path) -> None:
    """The BLOCK must rest on real column lineage, not on a bare decision string."""
    sql = (BLOCKED / "customers.sql").read_text(encoding="utf-8")
    model, project_root = _project(tmp_path, sql)

    result = _decide(model, project_root)

    radius = [
        evidence
        for finding in result["findings"]
        for evidence in finding["evidence"]
        if evidence["kind"] == "blast_radius"
    ]
    assert radius, "the blocked example must carry blast_radius evidence"
    detail = radius[0]["detail"]
    assert detail["downstream_count"] > 0
    assert detail["granularity"] == "column"
    assert detail["dashboards"], "the PII path must still reach a dashboard"


def test_the_flagship_radius_is_now_read_completely(tmp_path: Path) -> None:
    """No consumer in this radius may be silently unread.

    `critical_assets` and `cross_team_owners` are the fields `critical_downstream`
    blocks on, so a failed downstream read can quietly weaken a blocking verdict.
    The gate records every such failure in `unreadable_assets`; this asserts the
    flagship snapshot is complete, so the published BLOCK rests on a fully read
    radius rather than on a partial one that happened to look clean.
    The report-don't-hide mechanism itself is covered against an injected failing
    graph in `tests/test_gates_blast.py`.
    """
    sql = (BLOCKED / "customers.sql").read_text(encoding="utf-8")
    model, project_root = _project(tmp_path, sql)

    result = _decide(model, project_root)

    detail = next(
        evidence["detail"]
        for finding in result["findings"]
        for evidence in finding["evidence"]
        if evidence["kind"] == "blast_radius"
    )
    assert detail["unreadable_assets"] == []
    assert detail["cross_team_owners"], (
        "a completely read radius must expose the cross-team ownership it found"
    )


def test_the_published_policy_hash_matches_the_shipped_policy() -> None:
    """A stale policy_hash makes the README's reproduction command a lie."""
    published = _published()

    assert (
        published["policy_hash"]
        == hashlib.sha256(default_policy_path().read_bytes()).hexdigest()
    )


def test_the_committed_verdict_matches_a_fresh_engine_run() -> None:
    """The flagship artifact is generated, not curated; drift must break the build.

    This file drifted once already: the policy gained rules, its hash changed,
    and the published verdict kept citing the old one, so the reproduction
    command a judge would run no longer matched. Regenerating is now a script and
    this is its guard.
    """
    assert regenerate_example_01.rendered() == (BLOCKED / "verdict.json").read_text(
        encoding="utf-8"
    ), "examples/01 verdict.json is stale; rerun scripts/regenerate_example_01.py"


def test_the_committed_pr_comment_matches_a_fresh_render() -> None:
    """`pr-comment.md` is rendered output too; hand-editing it is how it drifted."""
    assert regenerate_example_01.comment() == (BLOCKED / "pr-comment.md").read_text(
        encoding="utf-8"
    ), "examples/01 pr-comment.md is stale; rerun scripts/regenerate_example_01.py"


@pytest.mark.parametrize(
    "artifact",
    ["README.md", "docs/PR-BOT.md", "examples/01-blocked-pii-dashboard/pr-comment.md"],
)
def test_every_artifact_publishing_the_hash_publishes_the_current_one(
    artifact: str,
) -> None:
    """Three files quote the reproduction command; all three must agree."""
    published_hash = _published()["policy_hash"]
    text = (ROOT / artifact).read_text(encoding="utf-8")

    assert f"policy_hash={published_hash}" in text, (
        f"{artifact} does not publish the current policy_hash"
    )


def test_a_change_that_touches_no_flagged_column_does_not_block(
    tmp_path: Path,
) -> None:
    """ENGINE-SPEC §7: the good-change half. Selecting one unflagged column is safe."""
    published = _published()
    safe_column = "customer_id"
    assert safe_column in published["touched"][0]["added_fields"]
    relation = published["touched"][0]["urn"].split(",")[1]
    model, project_root = _project(tmp_path, f"select {safe_column}\nfrom {relation}\n")

    result = _decide(model, project_root)

    assert result["decision"] != "BLOCK", (
        f"a single unflagged column must not block; got {result['findings']}"
    )


def test_the_engine_is_byte_deterministic_on_the_flagship_example(
    tmp_path: Path,
) -> None:
    sql = (BLOCKED / "customers.sql").read_text(encoding="utf-8")
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    first_model, first_root = _project(first, sql)
    second_model, second_root = _project(second, sql)

    first_result = _decide(first_model, first_root)
    second_result = _decide(second_model, second_root)

    assert json.dumps(first_result, sort_keys=True) == json.dumps(
        second_result, sort_keys=True
    )


@pytest.mark.parametrize(
    "artifact", ["README.md", "pr-comment.md", "verdict.json", "customers.sql"]
)
def test_every_published_example_file_is_present(artifact: str) -> None:
    """ENGINE-SPEC §7 calls these regression artifacts; a missing one is a broken link."""
    path = BLOCKED / artifact if artifact != "README.md" else BLOCKED / "README.md"
    assert path.exists(), f"{path} is referenced as published evidence"


def test_regenerating_does_not_leak_the_published_host_into_the_process() -> None:
    """The generator must not mutate the environment other tests read.

    It pins `SIDQ_DATAHUB_UI_URL` so published artifacts carry the public host, but
    this module is imported by the suite. A permanent mutation leaked into
    `test_cli.py`, which asserts the localhost default; the suite only passed
    because that file happens to sort first.
    """
    before = os.environ.get("SIDQ_DATAHUB_UI_URL")

    regenerate_example_01.rendered()

    assert os.environ.get("SIDQ_DATAHUB_UI_URL") == before


def test_the_generator_still_pins_the_public_host_inside_its_own_run() -> None:
    """Scoping the variable must not silently reintroduce localhost links."""
    verdict = regenerate_example_01.regenerate()

    links = [
        link
        for finding in verdict["findings"]
        for evidence in finding["evidence"]
        for link in evidence["graph_links"]
    ]
    assert links, "the published verdict must carry graph links"
    assert all(link.startswith("https://datahub.mlki.app") for link in links), (
        "published evidence must not point at localhost"
    )
