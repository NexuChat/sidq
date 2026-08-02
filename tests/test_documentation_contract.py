"""Contract tests for Sidq's two supported onboarding journeys."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SETUP = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
MCP = (ROOT / "docs" / "MCP-SERVER.md").read_text(encoding="utf-8")
OPERATIONS = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
DELIVERY = (ROOT / "docs" / "DELIVERY-SPEC.md").read_text(encoding="utf-8")
DEVPOST = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")
ENGINE = (ROOT / "docs" / "ENGINE-SPEC.md").read_text(encoding="utf-8")
RECEIPT = (ROOT / "docs" / "RECEIPT-SPEC.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
DEMO = (ROOT / "demo" / "README.md").read_text(encoding="utf-8")
RECEIPT_EXAMPLE = (ROOT / "examples" / "02-receipt-consumed" / "README.md").read_text(
    encoding="utf-8"
)
SKILL = (ROOT / "skills" / "datahub-verify" / "SKILL.md").read_text(encoding="utf-8")
SKILL_README = (ROOT / "skills" / "datahub-verify" / "README.md").read_text(
    encoding="utf-8"
)


def _combine(*documents: str) -> str:
    return "\n".join(documents)


def test_readme_leads_with_the_self_bootstrapping_offline_journey() -> None:
    section = README.split("## Try it in one command", maxsplit=1)[1]

    assert "make gate-demo" in section[:600]
    assert "https://github.com/NexuChat/sidq.git" in README
    assert "cd sidq" in README
    assert "make install" in README
    assert "no DataHub" in README
    assert "docs/SETUP.md" in README
    assert "docs/MCP-SERVER.md" in README


def test_connected_setup_is_complete_from_a_fresh_clone() -> None:
    for requirement in ("Python 3.12", "Docker", "Compose", "uv"):
        assert requirement in SETUP
    for command in (
        "git clone https://github.com/NexuChat/sidq.git",
        "cd sidq",
        "make mcp-install",
        "datahub docker quickstart",
        "make demo-stack",
        "make mcp-smoke",
        "make live-loop",
    ):
        assert command in SETUP
    assert "https://docs.astral.sh/uv/getting-started/installation/" in SETUP
    assert "curl --fail --silent --show-error http://localhost:8080/health" in SETUP
    assert "datahub init" in SETUP
    assert 'read -rsp "DataHub GMS token: " DATAHUB_GMS_TOKEN' in SETUP
    assert "401" in SETUP


def test_codex_can_attach_sidq_without_copying_secrets_into_config() -> None:
    install = (
        "codex mcp add sidq --env DATAHUB_GMS_URL=http://localhost:8080 "
        "--env SIDQ_REPO_ROOT=/absolute/path/to/data-repository -- "
        "/absolute/path/to/sidq/.venv/bin/sidq-mcp"
    )
    combined = _combine(README, MCP, SKILL, SKILL_README)

    assert install in combined
    assert "codex mcp list" in combined
    assert "`/mcp`" in combined
    assert "[mcp_servers.sidq]" in combined
    assert 'env_vars = ["DATAHUB_GMS_TOKEN", "SIDQ_POSTGRES_DSN"]' in combined
    assert "[mcp_servers.sidq.env]" in combined
    assert "Do not" in combined and "secret" in combined.lower()


def test_generic_mcp_client_uses_the_installed_absolute_command() -> None:
    generic_client = MCP.split("## Connect another MCP client", maxsplit=1)[1]

    assert '"command": "/absolute/path/to/sidq/.venv/bin/sidq-mcp"' in generic_client
    assert '"command": "sidq-mcp"' not in generic_client


def test_operations_check_both_deployed_runtime_locks_for_release_and_rollback() -> (
    None
):
    release, rollback = OPERATIONS.split("## Rollback", maxsplit=1)

    for procedure in (release, rollback):
        assert "requirements-landing.lock" in procedure
        assert "requirements-mcp.lock" in procedure
        assert "/opt/sidq/runtime/requirements-landing.lock" in procedure
        assert "/opt/sidq/runtime/requirements-mcp.lock" in procedure


def test_operations_can_adopt_only_a_complete_matching_legacy_runtime() -> None:
    adoption_at = OPERATIONS.index(
        "## One-time adoption of a legacy production runtime"
    )
    rebuild_at = OPERATIONS.index("## Rebuild the production runtime")
    release_at = OPERATIONS.index("## Release")
    assert adoption_at < rebuild_at < release_at

    adoption = OPERATIONS.split(
        "## One-time adoption of a legacy production runtime", maxsplit=1
    )[1].split("## Rebuild the production runtime", maxsplit=1)[0]
    rebuild = OPERATIONS[rebuild_at:release_at]
    release = OPERATIONS[release_at:].split("## Configuration and logs", maxsplit=1)[0]
    assert "one-time adoption" in rebuild.lower()
    assert "one-time adoption" in release.lower()
    release_writability_check = 'sudo find "$legacy_release" -perm /0222'
    runtime_writability_check = 'sudo find "/opt/sidq/runtime/$runtime_dir" -perm /0022'
    assert release_writability_check in adoption
    assert runtime_writability_check in adoption
    assert 'sudo find "$legacy_release" -perm /0022' not in adoption
    assert 'sudo find "/opt/sidq/runtime/$runtime_dir" -perm /0222' not in adoption

    for compatibility_input in (
        "requirements-dev.lock",
        "pyproject.toml",
        "uv.lock",
        "requirements-landing.lock",
        "requirements-mcp.lock",
    ):
        assert compatibility_input in adoption
    for required_check in (
        "compatibility_present=0",
        'sudo test "$compatibility_present" -eq 0',
        "legacy_release=$(readlink -f /opt/sidq/current)",
        "sudo stat -c '%U:%G' \"$legacy_release\"",
        'sudo find "$legacy_release" ! -user root',
        'sudo find "$legacy_release" -perm /0222',
        "/opt/sidq/runtime/venv/.sidq-dev-lock",
        "/opt/sidq/runtime/venv/bin/python",
        "/opt/sidq/runtime/mcp/bin/mcp-server-datahub",
        "--dry-run --no-index --require-hashes --no-deps",
        "systemctl is-active --quiet sidq-landing",
        "http://127.0.0.1:8766/healthz",
        "http://127.0.0.1:8766/readyz",
        "sudo install -o root -g root -m 0444",
        'sudo cmp --silent "$legacy_release/$compatibility_input"',
    ):
        assert required_check in adoption

    first_copy = adoption.index("sudo install -o root -g root -m 0444")
    assert adoption.index("systemctl is-active --quiet sidq-landing") < first_copy
    assert adoption.index("http://127.0.0.1:8766/readyz") < first_copy
    assert (
        adoption.index("--dry-run --no-index --require-hashes --no-deps") < first_copy
    )
    for forbidden_mutation in (
        "current.next",
        "systemctl restart",
        "systemctl stop",
        "systemctl start",
        "mv -Tf",
    ):
        assert forbidden_mutation not in adoption


def test_operations_provide_the_real_production_runtime_rebuild() -> None:
    rebuild = OPERATIONS.split("## Rebuild the production runtime", maxsplit=1)[
        1
    ].split("## Release", maxsplit=1)[0]

    for expected in (
        "/opt/sidq/runtime/venv",
        "/opt/sidq/runtime/mcp",
        "--require-hashes",
        "requirements-landing.lock",
        "requirements-mcp.lock",
        "--no-build-isolation --no-deps",
        "mcp-server-datahub --help",
    ):
        assert expected in rebuild
    assert "docs/SETUP.md" not in rebuild


def test_release_can_resume_an_exact_prepared_immutable_release() -> None:
    release = OPERATIONS.split("## Release", maxsplit=1)[1].split(
        "## Configuration and logs", maxsplit=1
    )[0]

    assert 'if sudo test -e "$release_dir" || sudo test -L "$release_dir"' in release
    assert 'sudo test ! -L "$release_dir"' in release
    assert "sudo stat -c '%U:%G' \"$release_dir\"" in release
    assert 'sudo find "$release_dir" ! -user root' in release
    assert 'sudo find "$release_dir" -perm /0222' in release
    assert "diff --recursive --brief --no-dereference" in release
    assert 'sudo rm -rf -- "$staging_dir"' in release
    assert "immutable release path already exists" not in release


def test_operations_define_swapped_runtime_recovery_and_cleanup() -> None:
    rebuild = OPERATIONS.split("## Rebuild the production runtime", maxsplit=1)[
        1
    ].split("## Recover or retire a swapped runtime", maxsplit=1)[0]
    recovery = OPERATIONS.split("## Recover or retire a swapped runtime", maxsplit=1)[
        1
    ].split("## Release", maxsplit=1)[0]

    assert "/opt/sidq/runtime/compatibility.previous" in rebuild
    for previous, active in (
        ("venv.previous", "venv"),
        ("mcp.previous", "mcp"),
    ):
        assert f"/opt/sidq/runtime/{previous}" in recovery
        assert f"/opt/sidq/runtime/{active}" in recovery
    assert '"/opt/sidq/runtime/compatibility.previous/$compatibility_input"' in recovery
    assert "sudo systemctl restart sidq-landing" in recovery
    assert "curl --fail --silent http://127.0.0.1:8766/healthz" in recovery
    assert "sudo rm -rf -- /opt/sidq/runtime/venv.previous" in recovery
    assert "sudo rm -rf -- /opt/sidq/runtime/mcp.previous" in recovery
    assert "sudo rm -rf -- /opt/sidq/runtime/venv.failed" in recovery
    assert "sudo rm -rf -- /opt/sidq/runtime/mcp.failed" in recovery


def test_skill_install_is_codex_specific_and_does_not_claim_to_attach_mcp() -> None:
    command = "npx skills add NexuChat/sidq --skill datahub-verify --agent codex"
    combined = _combine(README, DEVPOST, SKILL, SKILL_README)

    assert command in combined
    assert ".agents/skills/datahub-verify" in combined
    assert "does not install" in combined.lower()
    assert "MCP" in combined
    assert "make mcp-smoke" in combined


def test_skill_and_mcp_commands_name_their_distinct_repository_roots() -> None:
    skill_install = "npx skills add NexuChat/sidq --skill datahub-verify --agent codex"

    for document in (README, MCP, SKILL, SKILL_README):
        assert "cd /absolute/path/to/data-repository" in document
        assert skill_install in document
        assert (
            "/absolute/path/to/data-repository/.agents/skills/datahub-verify"
            in document
        )
        assert "cd /absolute/path/to/sidq" in document
        assert "make mcp-smoke" in document


def test_skill_worked_verdict_uses_the_real_finding_evidence_shape() -> None:
    actual = json.loads(
        (ROOT / "examples" / "01-blocked-pii-dashboard" / "verdict.json").read_text(
            encoding="utf-8"
        )
    )
    worked_section = SKILL.split(
        "### Worked example: blocked PII dashboard change", maxsplit=1
    )[1]
    worked = json.loads(
        worked_section.split("```json", maxsplit=1)[1].split("```", 1)[0]
    )

    assert worked["commit_sha"] == actual["commit_sha"]
    assert worked["decision"] == actual["decision"]
    assert worked["policy_hash"] == actual["policy_hash"]
    for finding in worked["findings"]:
        assert "kind" not in finding
        assert "subject" not in finding
        matching = next(
            item for item in actual["findings"] if item["rule_id"] == finding["rule_id"]
        )
        for key in ("message", "rule_id", "severity"):
            assert finding[key] == matching[key]
        assert finding["evidence"]
        for evidence in finding["evidence"]:
            actual_evidence = next(
                item
                for item in matching["evidence"]
                if item["kind"] == evidence["kind"]
                and item["subject"] == evidence["subject"]
            )
            assert evidence["graph_links"] == actual_evidence["graph_links"]
            for key, value in evidence["detail"].items():
                assert actual_evidence["detail"][key] == value


def test_datahub_and_sidq_mcp_servers_have_distinct_roles() -> None:
    combined = _combine(README, MCP, SETUP)

    assert "mcp-server-datahub" in combined
    assert "graph dependency" in combined
    assert "sidq-mcp" in combined
    for tool in ("check_change", "verify_context", "search_verified"):
        assert tool in combined
    assert "exactly three tools" in combined


def test_demo_compose_does_not_claim_to_supply_datahub() -> None:
    combined = _combine(SETUP, DEMO)

    assert "controlled PostgreSQL" in combined
    assert "already-running DataHub" in combined
    assert "datahub_network" in combined
    assert "does not start DataHub" in combined


def test_specs_describe_the_current_contract_without_stale_surfaces() -> None:
    owned = _combine(
        README,
        ARCHITECTURE,
        SETUP,
        MCP,
        DELIVERY,
        DEVPOST,
        ENGINE,
        RECEIPT,
        DEMO,
        RECEIPT_EXAMPLE,
        SKILL,
        SKILL_README,
    ).lower()

    for stale in (
        "(binding)",
        "one local graph",
        "assertion gate",
        "assertions.py",
        "static one file",
    ):
        assert stale not in owned

    assert "historical" in DELIVERY.lower()
    assert "superseded" in DELIVERY.lower()
    assert "dynamic" in DELIVERY.lower()
    assert "hosted" in DELIVERY.lower()


def test_receipt_consumption_names_cli_and_only_real_sidq_mcp_tools() -> None:
    combined = _combine(RECEIPT, RECEIPT_EXAMPLE)

    assert "sidq verify" in combined
    assert "get_verification_status" not in combined
    assert "check_change" in RECEIPT
    assert "verify_context" in RECEIPT
    assert "search_verified" in RECEIPT
    assert "fail closed" in RECEIPT.lower() or "fail-closed" in RECEIPT.lower()
