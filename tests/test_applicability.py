from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidq.bot.action import ActionError
from sidq.bot.applicability import classify_changes, safe_path


def trees(tmp_path: Path, paths: list[str]):
    base, head = tmp_path / "base", tmp_path / "head"
    for root in (base, head):
        root.mkdir()
        for name in paths:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("contents\n")
    inventory = dict.fromkeys(paths, "100644")
    return base, head, inventory


def classify(tmp_path, paths, records=None, mappings=None):
    base, head, inventory = trees(tmp_path, paths)
    if mappings:
        for root in (base, head):
            path = root / ".sidq/assets.yml"
            path.parent.mkdir()
            path.write_text(
                "assets:\n" + "".join(f"  {name}: urn:asset\n" for name in mappings)
            )
        inventory[".sidq/assets.yml"] = "100644"
    records = (
        records
        if records is not None
        else [{"filename": p, "status": "modified"} for p in paths]
    )
    return classify_changes(
        records,
        changed_count=len(records),
        base_root=base,
        head_root=head,
        base_inventory=inventory,
        head_inventory=inventory,
    )


def test_pr6_dependency_test_and_documentation_set_is_wholly_non_data(tmp_path):
    paths = [
        "uv.lock",
        "requirements.lock",
        "requirements-action.lock",
        "requirements-dev.lock",
        "requirements-bench.lock",
        "requirements-landing.lock",
        "tests/test_dependency_security.py",
        "README.md",
        "ARCHITECTURE.md",
        "docs/CLAIMS-MATRIX.md",
        "docs/SECURITY-AUDIT.md",
    ]
    result = classify(tmp_path, paths)
    assert result.kind == "non_data"
    assert result.categories == ("dependencies", "documentation", "test_code")


@pytest.mark.parametrize(
    "path", ["README.md", "tests/test_dependency_security.py", "web/app.js"]
)
def test_explicit_asset_mapping_precedes_supported_maintenance(tmp_path, path):
    assert classify(tmp_path, [path], mappings=[path]).kind == "data"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "action.yml",
        "pyproject.toml",
        "Makefile",
        "SECURITY.md",
        "src/sidq/policy/engine.py",
        "src/sidq/resolver.py",
        "scripts/helper.py",
        "deploy/config.yml",
        "tests/test_bot.py",
        "tests/conftest.py",
        "web/server.py",
        "unknown.txt",
        "requirements-mcp.lock",
    ],
)
def test_protected_or_unknown_files_cannot_be_exempted(tmp_path, path):
    assert classify(tmp_path, [path]).kind == "blocked"


def test_mapping_cannot_turn_a_protected_workflow_into_an_exemption(tmp_path):
    path = ".github/workflows/ci.yml"
    assert classify(tmp_path, [path], mappings=[path]).kind == "blocked"


def test_mixed_asset_and_documentation_is_blocked(tmp_path):
    assert classify(tmp_path, ["models/orders.sql", "README.md"]).kind == "blocked"


def test_only_assets_require_the_real_engine(tmp_path):
    assert classify(tmp_path, ["models/orders.sql"]).kind == "data"


def test_rename_from_sql_to_documentation_is_not_non_data(tmp_path):
    records = [
        {
            "filename": "README.md",
            "previous_filename": "models/orders.sql",
            "status": "renamed",
        }
    ]
    assert (
        classify(tmp_path, ["README.md", "models/orders.sql"], records).kind
        == "blocked"
    )


def test_deleted_asset_remains_in_strict_scope(tmp_path):
    base, head, inventory = trees(tmp_path, ["models/orders.sql"])
    (head / "models/orders.sql").unlink()
    result = classify_changes(
        [{"filename": "models/orders.sql", "status": "removed"}],
        changed_count=1,
        base_root=base,
        head_root=head,
        base_inventory=inventory,
        head_inventory={},
    )
    assert result.kind == "data"
    assert result.files == ("models/orders.sql",)


@pytest.mark.parametrize("status", ["added", "removed", "renamed"])
def test_maintenance_structural_changes_need_review(tmp_path, status):
    base, head, inventory = trees(tmp_path, ["README.md"])
    old, new = dict(inventory), dict(inventory)
    record = {"filename": "README.md", "status": status}
    if status == "added":
        old.clear()
        (base / "README.md").unlink()
    elif status == "removed":
        new.clear()
        (head / "README.md").unlink()
    else:
        record["previous_filename"] = "old.md"
        (base / "old.md").write_text("old")
        old["old.md"] = "100644"
    result = classify_changes(
        [record],
        changed_count=1,
        base_root=base,
        head_root=head,
        base_inventory=old,
        head_inventory=new,
    )
    assert result.kind == "blocked"


@pytest.mark.parametrize(
    "value",
    [
        "../README.md",
        "/README.md",
        "./README.md",
        "docs//x.md",
        "docs/../README.md",
        "a\\b",
        "a\nb",
        "",
        None,
    ],
)
def test_noncanonical_paths_are_rejected(value):
    with pytest.raises(ActionError):
        safe_path(value)


@pytest.mark.parametrize(
    "records,count",
    [
        ([], 0),
        ([{"filename": "README.md", "status": "modified"}], 2),
        ([{}, {}], 3000),
        ([{"filename": "README.md", "status": "copied"}], 1),
        (
            [
                {
                    "filename": "README.md",
                    "status": "modified",
                    "previous_filename": "other",
                }
            ],
            1,
        ),
    ],
)
def test_incomplete_or_ambiguous_metadata_refuses_certification(
    tmp_path, records, count
):
    base, head, inventory = trees(tmp_path, ["README.md"])
    with pytest.raises(ActionError):
        classify_changes(
            records,
            changed_count=count,
            base_root=base,
            head_root=head,
            base_inventory=inventory,
            head_inventory=inventory,
        )


def test_duplicate_records_are_rejected(tmp_path):
    record = {"filename": "README.md", "status": "modified"}
    with pytest.raises(ActionError):
        classify(tmp_path, ["README.md"], [record, record])


def test_symlink_file_is_rejected(tmp_path):
    base, head, inventory = trees(tmp_path, ["README.md"])
    new = {"README.md": "120000"}
    with pytest.raises(ActionError):
        classify_changes(
            [{"filename": "README.md", "status": "modified"}],
            changed_count=1,
            base_root=base,
            head_root=head,
            base_inventory=inventory,
            head_inventory=new,
        )


def test_parent_symlink_is_rejected_before_reading(tmp_path):
    base, head, inventory = trees(tmp_path, ["docs/CLAIMS-MATRIX.md"])
    (head / "docs/CLAIMS-MATRIX.md").unlink()
    (head / "docs").rmdir()
    (head / "docs").symlink_to(base / "docs", target_is_directory=True)
    with pytest.raises(ActionError):
        classify_changes(
            [{"filename": "docs/CLAIMS-MATRIX.md", "status": "modified"}],
            changed_count=1,
            base_root=base,
            head_root=head,
            base_inventory=inventory,
            head_inventory=inventory,
        )


@pytest.mark.parametrize("path_key", ["original_file_path", "path"])
def test_head_manifest_mapping_of_readme_takes_precedence(tmp_path, path_key):
    base, head, inventory = trees(tmp_path, ["README.md"])
    (head / "manifest.json").write_text(
        json.dumps({"nodes": {"x": {path_key: "README.md"}}})
    )
    new = {**inventory, "manifest.json": "100644"}
    result = classify_changes(
        [{"filename": "README.md", "status": "modified"}],
        changed_count=1,
        base_root=base,
        head_root=head,
        base_inventory=inventory,
        head_inventory=new,
    )
    assert result.kind == "data"


def test_malformed_mapping_refuses_non_data(tmp_path):
    base, head, inventory = trees(tmp_path, ["README.md", "manifest.json"])
    with pytest.raises((ActionError, ValueError)):
        classify_changes(
            [{"filename": "README.md", "status": "modified"}],
            changed_count=1,
            base_root=base,
            head_root=head,
            base_inventory=inventory,
            head_inventory=inventory,
        )


def test_empty_assets_map_keeps_resolver_top_level_fallback(tmp_path):
    base, head, inventory = trees(tmp_path, ["README.md"])
    for root in (base, head):
        (root / ".sidq").mkdir()
        (root / ".sidq/assets.yml").write_text("assets: {}\nREADME.md: urn:asset\n")
    inventory[".sidq/assets.yml"] = "100644"
    result = classify_changes(
        [{"filename": "README.md", "status": "modified"}],
        changed_count=1,
        base_root=base,
        head_root=head,
        base_inventory=inventory,
        head_inventory=inventory,
    )
    assert result.kind == "data"
