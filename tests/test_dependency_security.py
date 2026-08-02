"""Supply-chain invariants for the published, hash-locked environments."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
APPLICATION_LOCKS = (
    "requirements.lock",
    "requirements-action.lock",
    "requirements-bench.lock",
    "requirements-dev.lock",
    "requirements-landing.lock",
)


def _locked_version(filename: str, package: str) -> tuple[int, ...] | None:
    contents = (ROOT / filename).read_text(encoding="utf-8")
    match = re.search(
        rf"(?m)^{re.escape(package)}==([0-9]+(?:\.[0-9]+)*) \\\n", contents
    )
    return (
        None
        if match is None
        else tuple(int(part) for part in match.group(1).split("."))
    )


def test_setuptools_security_floor_covers_build_and_runtime_environments() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "setuptools>=83" in project["build-system"]["requires"]
    assert "setuptools>=83" in project["project"]["optional-dependencies"]["dev"]
    assert "setuptools>=83" in project["project"]["optional-dependencies"]["action"]

    for filename in APPLICATION_LOCKS:
        version = _locked_version(filename, "setuptools")
        assert version is None or version >= (83, 0, 0), filename

    assert _locked_version("requirements-mcp.lock", "setuptools") == (81, 0, 0)


def test_datahub_sdk_stays_out_of_application_locks() -> None:
    for filename in APPLICATION_LOCKS:
        contents = (ROOT / filename).read_text(encoding="utf-8")
        assert "acryl-datahub==" not in contents, filename

    mcp_source = (ROOT / "requirements-mcp.in").read_text(encoding="utf-8")
    mcp_lock = (ROOT / "requirements-mcp.lock").read_text(encoding="utf-8")
    assert "acryl-datahub==1.6.0.16" in mcp_source
    assert "acryl-datahub==1.6.0.16" in mcp_lock


def test_preflight_bench_has_an_isolated_hash_locked_environment() -> None:
    bench = (ROOT / "requirements-bench.lock").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.lock").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "scikit-learn==" in bench
    assert "--hash=sha256:" in bench
    assert "scikit-learn==" not in dev
    assert (
        "uv export --locked --extra bench --no-emit-project "
        "--output-file requirements-bench.lock"
    ) in makefile
    assert "BENCH_VENV ?= .venv-bench" in makefile
    assert "$(BENCH_VENV)/.sidq-bench-lock: requirements-bench.lock" in makefile
    assert "--require-hashes -r requirements-bench.lock" in makefile
    assert "$(BENCH_VENV)/bin/python scripts/train_preflight.py --check" in makefile
    assert "requirements-bench.lock" in security


def test_supply_chain_runbook_does_not_hide_the_setuptools_advisory() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "CVE-2026-59890" in security
    assert "PYSEC-2026-3447" in security
    assert "setuptools>=83" in security
    assert "setuptools<82" in security
    assert "remaining risk" in security
    assert "pip-audit" in security
    audit_commands = [
        line for line in security.splitlines() if line.startswith("uvx pip-audit")
    ]
    assert audit_commands
    assert all("--ignore-vuln" not in command for command in audit_commands)
