"""Regression tests for the landing page's concise onboarding journey."""

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
        self._inside_code = False
        self._code_parts: list[str] = []
        self._last_code = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "code":
            self._inside_code = True
            self._code_parts = []
        if tag == "button" and "data-copy" in attributes:
            attributes["visible-command"] = self._last_code
            self.buttons.append(attributes)
        if attributes.get("aria-live") == "polite" and attributes.get("id"):
            self.status_ids.add(attributes["id"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self._inside_code:
            self._last_code = "".join(self._code_parts).strip()
            self._inside_code = False

    def handle_data(self, data: str) -> None:
        if self._inside_code:
            self._code_parts.append(data)


def _landing() -> str:
    return HTML.read_text(encoding="utf-8")


def test_landing_leads_with_one_decision_then_the_independent_handoff() -> None:
    html = _landing()
    hero = html.split('<section class="hero', 1)[1].split("</section>", 1)[0]

    assert 'id="trust-loop"' in html
    assert 'id="decision"' in html
    assert 'id="agent-handoff"' in html
    assert 'id="install-connect"' in html
    assert html.index('id="trust-loop"') < html.index('id="decision"')
    assert html.index('id="decision"') < html.index('id="agent-handoff"')
    assert html.index('data-run="handoff"') < html.index('id="install-connect"')
    assert html.index('id="install-connect"') < html.index('class="deep-dives')
    assert "Agents trust DataHub before they act." in hero
    assert "who verifies that DataHub's context is telling the truth?" in hero
    assert hero.count('class="cta run"') == 1
    assert 'href="#trust-loop">See how trust is decided ↓</a>' in hero
    assert "One command. A local graph." not in html
    assert "Try the proof yourself." in html
    assert "No setup maze." in html
    assert "Dependencies download on the first run" in html
    assert "needs no DataHub or account" in html
    assert "demo-stack" in html and "live-loop" in html and "DataHub" in html
    assert "starts DataHub" not in html
    assert 'class="rail"' not in html


def test_receipt_handoff_and_mcp_tools_are_distinct_and_truthful() -> None:
    html = _landing()
    handoff = html.split('id="agent-handoff"', 1)[1].split("</section>", 1)[0]

    assert "An opted-in audit can write a reproducible receipt to DataHub." in handoff
    assert "A separate reader checks the graph, policy, and age again." in handoff
    assert "PASS continues. BLOCK, missing, or stale stops." in handoff
    assert "three read-only MCP tools" in handoff
    for tool in ("check_change", "verify_context", "search_verified"):
        assert f"<code>{tool}</code>" in handoff
    assert 'data-run="handoff"' in handoff
    assert "check_change writes" not in handoff


def test_general_trust_contract_precedes_the_pii_example() -> None:
    html = _landing()
    contract = html.split('id="trust-loop"', 1)[1].split("</section>", 1)[0]

    for stage in (
        "Context",
        "Evidence check",
        "PASS / WARN / BLOCK",
        "Receipt",
        "Next agent",
    ):
        assert stage in contract
    for evidence in ("schema", "lineage", "governance", "documented claims"):
        assert evidence in contract.lower()
    assert "when configured, the source itself" in contract
    assert "Missing evidence is named, never silently treated as clean." in contract


def test_install_journey_publishes_exact_copyable_commands() -> None:
    parser = _CopyButtonParser()
    parser.feed(_landing())
    commands = {button["data-copy"] for button in parser.buttons}

    assert commands == {
        (
            "git clone https://github.com/NexuChat/sidq.git\n"
            "cd sidq\n"
            "make install\n"
            "make gate-demo"
        ),
        "make mcp-install",
        (
            "codex mcp add sidq --env DATAHUB_GMS_URL=http://localhost:8080 "
            "--env SIDQ_REPO_ROOT=/absolute/path/to/data-repository -- "
            "/absolute/path/to/sidq/.venv/bin/sidq-mcp"
        ),
        "make mcp-smoke",
        (
            "cd /absolute/path/to/data-repository\n"
            "npx skills add NexuChat/sidq --skill datahub-verify --agent codex"
        ),
        "make demo-stack && make live-loop",
    }
    assert all(
        button["visible-command"] == button["data-copy"] for button in parser.buttons
    )
    assert len(parser.buttons) == len(
        {button["aria-describedby"] for button in parser.buttons}
    )
    assert all(
        button["aria-describedby"] in parser.status_ids for button in parser.buttons
    )


def test_offline_and_connected_commands_are_in_executable_order() -> None:
    html = _landing()
    offline = html.split('<li class="start-step offline">', 1)[1].split("</li>", 1)[0]
    connected = html.split("<strong>Connect Codex + DataHub</strong>", 1)[1].split(
        "</details>", 1
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
    assert connected.index("make mcp-install") < connected.index("codex mcp add sidq")
    assert connected.index("codex mcp add sidq") < connected.index("make mcp-smoke")


def test_skill_smoke_and_config_name_their_required_working_directories() -> None:
    html = _landing()
    skill = html.split("<strong>Workflow skill &amp; safe config</strong>", 1)[1].split(
        "</details>", 1
    )[0]
    connected = html.split("<strong>Connect Codex + DataHub</strong>", 1)[1].split(
        "</details>", 1
    )[0]

    assert skill.index("cd /absolute/path/to/data-repository") < skill.index(
        "npx skills add"
    )
    assert "From the Sidq clone" in connected
    assert "make mcp-smoke" in connected
    assert "Keep this in the trusted target data repository" in html
    assert "Secret values are absent" in html


def test_codex_connection_shows_the_in_client_mcp_check() -> None:
    html = _landing()
    connected = html.split("<strong>Connect Codex + DataHub</strong>", 1)[1].split(
        "</details>", 1
    )[0]

    assert "codex mcp add sidq" in connected
    assert connected.index("codex mcp list") < connected.index("/mcp")
    assert "Codex → Sidq → DataHub MCP → GMS." in connected


def test_secret_guidance_uses_env_passthrough_and_links_primary_docs() -> None:
    html = _landing()

    assert 'env_vars = ["DATAHUB_GMS_TOKEN", "SIDQ_POSTGRES_DSN"]' in html
    assert 'DATAHUB_GMS_URL = "http://localhost:8080"' in html
    assert 'SIDQ_REPO_ROOT = "/absolute/path/to/data-repository"' in html
    assert 'cwd = "/absolute/path/to/data-repository"' in html
    assert "DATAHUB_GMS_TOKEN =" not in html
    assert "SIDQ_POSTGRES_DSN =" not in html
    assert "stay in your shell, service manager, or secret store" in html
    assert "Instructions for the workflow; not connectivity." in html
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
    assert re.search(
        r"\.start-path,\s*\.install-details\s*\{[^}]*grid-template-columns:\s*1fr",
        mobile,
        flags=re.DOTALL,
    )
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


def test_primary_setup_shows_one_offline_proof_and_hides_connected_depth() -> None:
    html = _landing()
    install = html.split('id="install-connect"', 1)[1].split(
        '<section class="deep-dives', 1
    )[0]

    assert install.count('class="start-step offline"') == 1
    assert "Replay the proof." in install
    assert "Attach Sidq to Codex." in install
    assert "Verify the whole chain." in install
    assert install.count("<details>") == 2
    assert "<details open" not in install
    assert install.index("Replay the proof.") < install.index("<details>")
    assert install.index("Need DataHub?") < install.index("make mcp-install")
    for removed_clutter in (
        "connected-sequence",
        "install-grid",
        "install-step",
        "Start the pinned DataHub OSS quickstart",
        "uv tool install --with",
        "command -v datahub",
        "scripts/smoke_mcp.py",
    ):
        assert removed_clutter not in install
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
