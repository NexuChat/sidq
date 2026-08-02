from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from scripts import smoke_mcp, smoke_sidq_mcp

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def target_body(name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}:[^\n]*\n(?P<body>(?:\t.*\n|#.*\n|\n)*)",
        MAKEFILE,
        flags=re.MULTILINE,
    )
    assert match is not None, f"missing make target: {name}"
    return match.group("body")


def test_install_builds_the_locked_project_environment_and_prints_both_clis() -> None:
    body = target_body("install")

    assert "$(VENV)/.sidq-dev-lock" in MAKEFILE.split("install:", 1)[1].splitlines()[0]
    assert "--require-hashes -r requirements-dev.lock" in MAKEFILE
    assert "$(VENV)/bin/sidq" in body
    assert "$(VENV)/bin/sidq-mcp" in body
    assert "test -x" in body
    assert "incomplete" in body.lower()


def test_bootstrap_uses_python_312_consistently() -> None:
    marker = MAKEFILE.split("$(VENV)/.sidq-dev-lock:", 1)[1].split(
        "$(BENCH_VENV)/.sidq-bench-lock:", 1
    )[0]

    assert "PYTHON ?= python3.12" in MAKEFILE
    assert "$(PYTHON) -m venv $(VENV)" in marker
    assert "$(PYTHON) -m venv $(BENCH_VENV)" in MAKEFILE
    assert "Python 3.12" in marker
    assert "python3 -m venv" not in MAKEFILE


def test_install_rejects_a_current_marker_with_missing_clis(tmp_path: Path) -> None:
    venv = tmp_path / "damaged-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".sidq-dev-lock").touch()

    completed = subprocess.run(
        ["make", f"VENV={venv}", "install"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "is incomplete" in completed.stderr


def test_mcp_install_uses_the_official_server_in_an_isolated_uv_tool() -> None:
    body = target_body("mcp-install")

    assert "command -v uv" in body
    assert "install uv" in body.lower()
    assert (
        "uv tool install --force --with acryl-datahub==1.6.0.16 "
        "--with-executables-from acryl-datahub mcp-server-datahub==0.6.0"
    ) in body
    assert "command -v datahub" in body
    assert "command -v mcp-server-datahub" in body
    assert "uv tool dir --bin" in body
    assert "shadow" in body.lower()
    assert '"mcp>=2,<3"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_demo_stack_checks_only_its_real_external_dependencies() -> None:
    prereqs = target_body("demo-prereqs")
    stack = target_body("demo-stack")

    assert "docker compose version" in prereqs
    assert "docker info" in prereqs
    assert "/health" in prereqs
    assert "docker network inspect datahub_network" in prereqs
    assert "datahub-gms-quickstart:8080/health" in prereqs
    assert "/api/graphql" in prereqs
    assert "DataHub catalog authentication failed" in prereqs
    assert "mcp-server-datahub" not in prereqs
    assert "demo-prereqs" in MAKEFILE.split("demo-stack:", 1)[1].splitlines()[0]
    assert "$(MAKE) demo-up" in stack
    assert "$(MAKE) demo-ingest" in stack
    assert "quickstart" in MAKEFILE.lower()


def test_demo_ingest_passes_the_gms_token_by_name_without_leaking_it() -> None:
    body = target_body("demo-ingest")
    recipe = (ROOT / "demo/ingest.dhub.yaml").read_text(encoding="utf-8")
    secret = "setup-contract-secret-value"
    dry_run = subprocess.run(
        ["make", "-n", f"DATAHUB_GMS_TOKEN={secret}", "demo-ingest"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--env DATAHUB_GMS_TOKEN" in body
    assert "${DATAHUB_GMS_TOKEN:-}" in recipe
    assert "authentication failed" in body.lower()
    assert ">/dev/null 2>&1" in body
    assert secret not in dry_run.stdout + dry_run.stderr


def test_demo_up_repairs_only_the_controlled_role_then_tests_remote_tcp() -> None:
    body = target_body("demo-up")

    assert "ALTER ROLE sidq" in body
    assert "sidq-demo-postgres" in body
    assert "postgres:16-alpine" in body
    assert "PGPASSWORD" in body
    assert "stale" in body.lower()
    assert "down --volumes" not in body


def test_mcp_smoke_preflights_connected_mode_and_uses_the_project_python() -> None:
    body = target_body("mcp-smoke")

    assert (
        "$(VENV)/.sidq-dev-lock" in MAKEFILE.split("mcp-smoke:", 1)[1].splitlines()[0]
    )
    assert "the pinned uv mcp-server-datahub is missing" in body
    assert "/health" in body
    assert "/api/graphql" in body
    assert "DataHub catalog authentication failed" in body
    assert "--config -" in body
    assert "uv tool dir --bin" in body
    assert 'PATH="$$tool_bin:$$PATH"' in body
    assert "DATAHUB_GMS_URL=$(DATAHUB_GMS_URL)" in body
    assert "$(VENV)/bin/python scripts/smoke_sidq_mcp.py" in body
    assert "$(VENV)/bin/python scripts/smoke_mcp.py" in body
    assert body.index("scripts/smoke_sidq_mcp.py") < body.index("scripts/smoke_mcp.py")
    assert "set -e;" in body


def test_datahub_mcp_decoder_uses_the_current_structured_content_field() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="not JSON")],
        structured_content={"result": {"total": 1}},
        is_error=False,
    )

    assert smoke_mcp.decode_result("search", result) == {"total": 1}


def test_sidq_mcp_smoke_calls_verify_context_through_the_real_chain() -> None:
    script = ROOT / "scripts/smoke_sidq_mcp.py"
    source = script.read_text(encoding="utf-8")

    assert "ClientSession" in source
    assert "stdio_client" in source
    assert "session.initialize()" in source
    assert "session.list_tools()" in source
    assert 'session.call_tool("verify_context"' in source
    assert "SIDQ_VERIFICATION_STORE" in source
    assert "TemporaryDirectory" in source
    assert "GRAPH_UNAVAILABLE" in source
    assert "check_change" in source
    assert "verify_context" in source
    assert "search_verified" in source

    summary = smoke_sidq_mcp.verification_summary(
        {
            "urn": smoke_sidq_mcp.SHOWCASE_URN,
            "truthful": False,
            "findings": [],
            "unverifiable": [{"check": "schema_drift"}],
        }
    )
    assert summary == {
        "urn": smoke_sidq_mcp.SHOWCASE_URN,
        "truthful": False,
        "findings": 0,
        "unverifiable": 1,
    }


def test_sidq_mcp_smoke_fails_closed_without_leaking_graph_error_details() -> None:
    secret = "graph-error-secret"

    with pytest.raises(RuntimeError) as raised:
        smoke_sidq_mcp.verification_summary(
            {
                "urn": smoke_sidq_mcp.SHOWCASE_URN,
                "truthful": False,
                "findings": [],
                "unverifiable": [],
                "error": {
                    "code": "GRAPH_UNAVAILABLE",
                    "message": "catalog failed",
                    "details": {"credential": secret},
                },
            }
        )

    assert "GRAPH_UNAVAILABLE" in str(raised.value)
    assert secret not in str(raised.value)


def test_doctor_is_read_only_and_reports_each_connected_mode_layer() -> None:
    body = target_body("doctor")

    for layer in (
        "project environment",
        "Docker engine",
        "Docker Compose",
        "DataHub GMS",
        "DataHub catalog access",
        "datahub_network",
        "DataHub CLI",
        "DataHub MCP server",
        "Codex CLI",
    ):
        assert layer in body
    assert "optional" in body.lower()
    assert "/api/graphql" in body
    assert "DATAHUB_GMS_TOKEN" in body
    assert "--config -" in body
    for mutation in (" compose up", " compose down", "docker run", "uv tool install"):
        assert mutation not in body
    assert "missing" in body.lower()

    secret = "doctor-contract-secret-value"
    dry_run = subprocess.run(
        ["make", "-n", f"DATAHUB_GMS_TOKEN={secret}", "doctor"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert secret not in dry_run.stdout + dry_run.stderr


def test_operations_uses_drop_ins_for_non_secrets_and_credentials_for_secrets() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    service = (ROOT / "deploy/sidq-landing.service").read_text(encoding="utf-8")

    assert "/etc/sidq/landing.env" not in operations
    assert "systemctl edit sidq-landing" in operations
    assert "sidq-landing.service.d/override.conf" in operations
    assert "[Service]" in operations
    assert "SIDQ_ALLOWED_ORIGINS=https://sidq.mlki.app" in operations
    assert "systemctl daemon-reload" in operations
    assert "systemctl restart sidq-landing" in operations
    assert "LoadCredential" in operations
    assert "EnvironmentFile=" not in service
    assert "Environment=SIDQ_ALLOWED_ORIGINS=https://sidq.mlki.app" in service
