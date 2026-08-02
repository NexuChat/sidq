"""Regression tests for the landing page install and local-connect journey."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = ROOT / "web" / "index.html"
SCRIPT = ROOT / "web" / "app.js"
STYLES = ROOT / "web" / "styles.css"


class _CopyButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.buttons: list[dict[str, str]] = []
        self.status_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "button" and "data-copy" in attributes:
            self.buttons.append(attributes)
        if attributes.get("aria-live") == "polite" and attributes.get("id"):
            self.status_ids.add(attributes["id"])


def _landing() -> str:
    return HTML.read_text(encoding="utf-8")


def test_install_and_connect_appears_before_the_runnable_demos() -> None:
    html = _landing()

    assert 'id="install-connect"' in html
    assert html.index('id="install-connect"') < html.index('data-run="handoff"')
    assert "Install &amp; connect" in html
    assert "One command. A local graph." not in html
    assert "The first install downloads dependencies" in html
    assert "After the hash-locked bootstrap" in html
    assert "replay itself needs no DataHub, network access, or credentials" in html
    assert "demo-stack" in html and "live-loop" in html and "DataHub" in html
    assert "starts DataHub" not in html
    for number, target, label in (
        ("01", "install-connect", "Install and connect"),
        ("02", "s02", "The system"),
        ("03", "s03", "Real engine output"),
        ("04", "s04", "The sample"),
        ("05", "s05", "The agents"),
        ("06", "s06", "Live demos"),
        ("07", "s07", "Operations"),
    ):
        assert (
            f'<a href="#{target}" aria-label="{number} — {label}">{number}</a>' in html
        )


def test_install_journey_publishes_exact_copyable_commands() -> None:
    parser = _CopyButtonParser()
    parser.feed(_landing())
    commands = {button["data-copy"] for button in parser.buttons}

    expected = {
        "git clone https://github.com/NexuChat/sidq.git",
        "cd sidq",
        "make install",
        "make mcp-install",
        "make gate-demo",
        (
            "uv tool install --with acryl-datahub==1.6.0.16 "
            "--with-executables-from acryl-datahub mcp-server-datahub==0.6.0"
        ),
        "command -v datahub && command -v mcp-server-datahub",
        (
            "codex mcp add sidq --env DATAHUB_GMS_URL=http://localhost:8080 "
            "--env SIDQ_REPO_ROOT=/absolute/path/to/data-repository -- "
            "/absolute/path/to/sidq/.venv/bin/sidq-mcp"
        ),
        "codex mcp list",
        "npx skills add NexuChat/sidq --skill datahub-verify --agent codex",
        ("DATAHUB_GMS_URL=http://localhost:8080 .venv/bin/python scripts/smoke_mcp.py"),
        "make mcp-smoke",
        "make demo-stack && make live-loop",
    }
    assert expected <= commands
    assert len(parser.buttons) == len(
        {button["aria-describedby"] for button in parser.buttons}
    )
    assert all(
        button["aria-describedby"] in parser.status_ids for button in parser.buttons
    )


def test_offline_and_connected_commands_are_in_executable_order() -> None:
    html = _landing()
    offline = html.split('<article class="install-step offline">', 1)[1].split(
        "</article>", 1
    )[0]
    connected = html.split('<article class="install-step connected">', 1)[1].split(
        "</article>", 1
    )[0]

    offline_commands = (
        "git clone https://github.com/NexuChat/sidq.git",
        "cd sidq",
        "make install",
        "make gate-demo",
    )
    assert [offline.index(command) for command in offline_commands] == sorted(
        offline.index(command) for command in offline_commands
    )
    assert "make mcp-install" not in offline
    assert connected.index("make mcp-install") < connected.index("uv tool install")


def test_skill_smoke_and_config_name_their_required_working_directories() -> None:
    html = _landing()
    skill = html.split('<article class="install-step companion">', 1)[1].split(
        "</article>", 1
    )[0]
    smoke = html.split('<article class="install-step smoke">', 1)[1].split(
        "</article>", 1
    )[0]

    assert skill.index("cd /absolute/path/to/data-repository") < skill.index(
        "npx skills add"
    )
    assert smoke.index("cd /absolute/path/to/sidq") < smoke.index("make mcp-smoke")
    assert "Place this file in the trusted target data repository" in html
    assert "not necessarily in the Sidq clone" in html


def test_codex_connection_shows_the_in_client_mcp_check() -> None:
    html = _landing()
    codex = html.split('<article class="install-step codex-connect">', 1)[1].split(
        "</article>", 1
    )[0]

    assert codex.index("codex mcp list") < codex.index('data-copy="/mcp"')
    assert "Inside Codex: /mcp" in codex
    assert "verify all three Sidq tools" in codex


def test_secret_guidance_uses_env_passthrough_and_links_primary_docs() -> None:
    html = _landing()

    assert 'env_vars = ["DATAHUB_GMS_TOKEN", "SIDQ_POSTGRES_DSN"]' in html
    assert 'DATAHUB_GMS_URL = "http://localhost:8080"' in html
    assert 'SIDQ_REPO_ROOT = "/absolute/path/to/data-repository"' in html
    assert 'cwd = "/absolute/path/to/data-repository"' in html
    assert "DATAHUB_GMS_TOKEN =" not in html
    assert "SIDQ_POSTGRES_DSN =" not in html
    assert "must not go in the repository" in html
    assert "The skill teaches the workflow; it does not replace the MCP server." in html
    for url in (
        "https://github.com/NexuChat/sidq/blob/main/docs/SETUP.md",
        "https://github.com/NexuChat/sidq/blob/main/docs/MCP-SERVER.md",
        "https://developers.openai.com/codex/mcp/",
    ):
        assert f'href="{url}"' in html


def test_copy_script_handles_every_button_and_keeps_server_errors() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'querySelectorAll("[data-copy]")' in script
    assert "button.dataset.copy" in script
    assert 'document.execCommand("copy")' in script
    assert "clipboard-proxy" in script
    assert ".style." not in script
    assert "innerHTML" not in script
    assert "catch (error)" in script
    assert "error.message" in script


def test_mobile_hero_and_command_bidi_have_explicit_layout_guards() -> None:
    styles = STYLES.read_text(encoding="utf-8")
    mobile = styles[styles.rindex("@media (max-width: 780px)") :]

    assert re.search(r"\.hero\s*\{[^}]*padding:", mobile, flags=re.DOTALL)
    assert re.search(r"\.hero-note\s*\{[^}]*margin-top:", mobile, flags=re.DOTALL)
    code_guard = re.search(r"code,\s*pre\s*\{(?P<body>[^}]*)\}", styles)
    assert code_guard is not None
    for declaration in (
        "direction: ltr",
        "text-align: left",
        "unicode-bidi: isolate",
    ):
        assert declaration in code_guard.group("body")


def test_non_json_http_errors_keep_status_without_exposing_response_html() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "readJsonResponse" in script
    assert 'response.headers.get("content-type")' in script
    assert "HTTP ${response.status}" in script
    assert "response.text()" not in script
    assert "capabilityResponse.json()" not in script
    assert script.count("response.json()") == 1


def test_run_status_distinguishes_findings_from_operational_failures() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "result.exit_code === 1" in script
    assert "findings, not an operational failure" in script
    assert "Operational failure" in script


def test_connected_sequence_matches_the_supported_bootstrap_and_datahub_order() -> None:
    html = _landing()
    sequence = (
        "Clone Sidq, enter the repository, and run make install",
        "Run make mcp-install",
        "Start the pinned DataHub OSS quickstart",
        "Run datahub init",
        "Load the showcase-ecommerce datapack",
        "Run make demo-stack",
        "Run make mcp-smoke",
        "Run make live-loop",
    )

    assert all(step in html for step in sequence)
    assert [html.index(step) for step in sequence] == sorted(
        html.index(step) for step in sequence
    )
    assert "Bootstrap Sidq. Then connect DataHub." in html
    assert "DataHub first. Sidq second." not in html
    assert re.search(
        r"<li><span>08</span><strong>Run make live-loop</strong></li>\s*</ol>", html
    )
    assert '<a href="https://github.com/NexuChat/sidq/blob/main/docs/SETUP.md">' in html


def test_local_origins_are_safe_defaults_while_production_is_explicit() -> None:
    from web import server

    assert server.DEFAULT_ALLOWED_ORIGINS == (
        "https://sidq.mlki.app",
        "http://127.0.0.1:8766",
        "http://localhost:8766",
    )
    unit = (ROOT / "deploy" / "sidq-landing.service").read_text(encoding="utf-8")
    assert "Environment=SIDQ_ALLOWED_ORIGINS=https://sidq.mlki.app" in unit
    assert "*" not in server.DEFAULT_ALLOWED_ORIGINS


def test_evidence_and_mobile_architecture_have_clear_open_affordances() -> None:
    html = _landing()

    for url in (
        "https://github.com/NexuChat/sidq/blob/main/ARCHITECTURE.md",
        (
            "https://github.com/NexuChat/sidq/blob/main/examples/"
            "01-blocked-pii-dashboard/verdict.json"
        ),
        "https://github.com/NexuChat/sidq/blob/main/docs/TRUTH-REPORT.md",
        (
            "https://github.com/NexuChat/sidq/blob/main/examples/"
            "03-catalog-truth-report/report.json"
        ),
    ):
        assert f'href="{url}"' in html
    assert 'class="architecture-frame"' in html
    assert 'class="architecture-open"' in html
    assert 'href="architecture.svg?v=' in html
