"""Repository maintenance scope, never an asset or software safety verdict.

This module is deliberately conservative and is executed from the trusted base.
The generic action and deterministic policy engine retain their strict behavior.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from sidq.bot.action import ActionError

LOCKS = frozenset(
    {
        "uv.lock",
        "requirements.lock",
        "requirements-action.lock",
        "requirements-bench.lock",
        "requirements-dev.lock",
        "requirements-landing.lock",
    }
)
# Existing files only: adding a path never silently expands trusted scope.
ROOT_DOCS = frozenset({"README.md", "ARCHITECTURE.md", "CONTRIBUTING.md"})
PROTECTED_DOCS = frozenset(
    {
        "docs/OPERATIONS.md",
        "docs/SETUP.md",
        "docs/PR-BOT.md",
        "docs/REPOSITORY-GATE.md",
        "docs/ENGINE-SPEC.md",
        "docs/MCP-CONTRACT.md",
        "docs/RECEIPT-SPEC.md",
    }
)
# These test modules define or protect enforcement, authentication, or publishing.
PROTECTED_TESTS = frozenset(
    {
        "tests/test_bot.py",
        "tests/test_applicability.py",
        "tests/test_ci_attestation.py",
        "tests/test_repository_gate.py",
        "tests/test_policy_engine.py",
        "tests/test_resolver.py",
        "tests/test_web_security.py",
        "tests/test_setup_contract.py",
        "tests/test_documentation_contract.py",
        "tests/test_skill_contract.py",
        "tests/test_packaging.py",
        "tests/test_published_claims.py",
    }
)
_MAX_FILE = 5 * 1024 * 1024


@dataclass(frozen=True)
class Applicability:
    kind: Literal["data", "non_data", "blocked"]
    categories: tuple[str, ...]
    files: tuple[str, ...]
    reason: str


def safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ActionError("invalid repository path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ActionError("unsafe repository path")
    return value


def tracked_files(root: Path) -> dict[str, str]:
    """Read Git's index as data; never import or execute proposed source files."""
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "-z"],
        check=True,
        capture_output=True,
    )
    result = {}
    for record in completed.stdout.decode("utf-8").split("\0"):
        if not record:
            continue
        metadata, filename = record.split("\t", 1)
        mode, _, stage = metadata.split()
        if stage != "0" or filename in result:
            raise ActionError("ambiguous checkout index")
        result[safe_path(filename)] = mode
    return result


def read_file(root: Path, filename: str) -> str:
    path = root / safe_path(filename)
    if any(item.is_symlink() for item in (path, *path.parents) if item != root.parent):
        raise ActionError("symlinked scope input")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ActionError("scope input is not a regular repository file")
    if path.stat().st_size > _MAX_FILE:
        raise ActionError("scope input exceeds size limit")
    return path.read_text(encoding="utf-8")


def mapped_paths(root: Path, inventory: Mapping[str, str]) -> set[str]:
    """Asset mappings in either tree override every maintenance category."""
    mapped: set[str] = set()
    for filename, mode in inventory.items():
        path = PurePosixPath(filename)
        is_assets = (
            path.name in {"assets.yml", "assets.yaml"} and path.parent.name == ".sidq"
        )
        if not is_assets and path.name != "manifest.json":
            continue
        if mode != "100644":
            raise ActionError("non-regular asset mapping")
        raw = read_file(root, filename)
        document = yaml.safe_load(raw) if is_assets else json.loads(raw)
        if not isinstance(document, Mapping):
            raise ActionError("malformed asset mapping")
        if is_assets:
            if "naming" in document or "naming_convention" in document:
                raise ActionError("custom naming convention requires strict assessment")
            mappings = document.get("assets", document.get("path_to_urn", {}))
            if not mappings:
                mappings = {
                    key: value
                    for key, value in document.items()
                    if isinstance(value, str)
                }
            if not isinstance(mappings, Mapping):
                raise ActionError("malformed explicit asset map")
            if any(not isinstance(value, str) for value in mappings.values()):
                raise ActionError("malformed explicit asset map entry")
            project = path.parent.parent
            names = list(mappings)
        else:
            # ReplayGraphClient's recording manifest is not a dbt asset map.
            nodes = document.get("nodes", {})
            if not isinstance(nodes, Mapping):
                raise ActionError("malformed dbt manifest")
            project = (
                path.parent.parent if path.parent.name == "target" else path.parent
            )
            names = []
            for node in nodes.values():
                if not isinstance(node, Mapping):
                    raise ActionError("malformed dbt node")
                source = node.get("original_file_path") or node.get("path")
                if source is not None:
                    names.append(source)
        for name in names:
            relative = safe_path(name)
            mapped.add(safe_path((project / relative).as_posix()))
    return mapped


def _category(filename: str, mapped: set[str]) -> str:
    path = PurePosixPath(filename)
    if (
        filename.startswith((".github/", "src/", "scripts/", "deploy/", "skills/"))
        or filename
        in {
            "action.yml",
            "Makefile",
            "pyproject.toml",
            "SECURITY.md",
            "web/server.py",
            "tests/conftest.py",
            "tests/__init__.py",
        }
        or filename in PROTECTED_TESTS
        or filename in PROTECTED_DOCS
        or ".sidq" in path.parts
    ):
        return "protected"
    if filename in mapped or path.suffix.lower() in {
        ".sql",
        ".csv",
        ".parquet",
        ".avro",
    }:
        return "data"
    if filename.startswith(("data/", "demo/", "examples/", "tests/fixtures/")):
        return "data"
    if ".sidq" in path.parts or path.name in {
        "manifest.json",
        "dbt_project.yml",
        "sources.yml",
        "schema.yml",
    }:
        return "protected"
    if filename in LOCKS:
        return "dependencies"
    if filename in ROOT_DOCS:
        return "documentation"
    if (
        filename.startswith("docs/")
        and path.suffix == ".md"
        and filename not in PROTECTED_DOCS
    ):
        return "documentation"
    # Runtime/enforcement source, scripts, deployment, auth and build settings
    # are deliberately unsupported in this first rollout, not broadly exempt.
    if filename in {"web/index.html", "web/scope.html", "web/styles.css", "web/app.js"}:
        return "presentation_code"
    if filename in {
        "tests/test_dependency_security.py",
        "tests/test_claim_reader.py",
        "tests/test_reader_calibration.py",
        "tests/test_datasheet.py",
        "tests/test_landing_experience.py",
    }:
        return "test_code"
    return "protected_or_unknown"


def classify_changes(
    records: Sequence[Mapping[str, Any]],
    *,
    changed_count: int,
    base_root: Path,
    head_root: Path,
    base_inventory: Mapping[str, str] | None = None,
    head_inventory: Mapping[str, str] | None = None,
) -> Applicability:
    """Only complete, known, regular-file maintenance modifications qualify."""
    if (
        type(changed_count) is not int
        or not 0 < changed_count < 3000
        or len(records) != changed_count
    ):
        raise ActionError("empty, incomplete, or oversized changed-file list")
    base = tracked_files(base_root) if base_inventory is None else base_inventory
    head = tracked_files(head_root) if head_inventory is None else head_inventory
    mapped = mapped_paths(base_root, base) | mapped_paths(head_root, head)
    paths: list[str] = []
    categories: set[str] = set()
    non_data_modifications_only = True
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ActionError("malformed changed-file record")
        filename = safe_path(record.get("filename"))
        if filename in seen:
            raise ActionError("duplicate changed-file record")
        seen.add(filename)
        status = record.get("status")
        if status not in {"added", "modified", "removed", "renamed"}:
            raise ActionError("unsupported changed-file status")
        previous = record.get("previous_filename")
        if (status == "renamed") != (previous is not None):
            raise ActionError("ambiguous rename metadata")
        if status == "added" and (filename in base or filename not in head):
            raise ActionError("added-file metadata disagrees with checkout")
        if status == "removed" and (filename not in base or filename in head):
            raise ActionError("removed-file metadata disagrees with checkout")
        if status == "modified" and (filename not in base or filename not in head):
            raise ActionError("modified-file metadata disagrees with checkout")
        if status == "renamed" and (previous not in base or filename not in head):
            raise ActionError("renamed-file metadata disagrees with checkout")
        affected = [filename] + ([safe_path(previous)] if previous is not None else [])
        for name in affected:
            for inventory in (base, head):
                if name in inventory and inventory[name] not in {"100644", "100755"}:
                    raise ActionError("symlink or non-regular changed file")
            categories.add(_category(name, mapped))
            paths.append(name)
        if (
            status != "modified"
            or filename not in base
            or filename not in head
            or base[filename] != head[filename]
        ):
            non_data_modifications_only = False
        else:
            # Check both trees, including parent symlinks and bounded file size.
            read_file(base_root, filename)
            read_file(head_root, filename)
    files = tuple(sorted(set(paths)))
    kinds = tuple(sorted(categories))
    if categories == {"data"}:
        return Applicability(
            "data", kinds, files, "Only data inputs changed; strict engine required."
        )
    supported = {"dependencies", "documentation", "test_code", "presentation_code"}
    if categories <= supported and non_data_modifications_only:
        return Applicability(
            "non_data",
            kinds,
            files,
            "Only supported, existing non-data files were modified.",
        )
    return Applicability(
        "blocked",
        kinds,
        files,
        "Mixed data/non-data, protected, unknown, renamed, added or deleted maintenance files require review.",
    )
