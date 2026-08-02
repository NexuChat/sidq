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
    assert "DataHub-native trust infrastructure" in hero
    assert "Don’t just make the agent smarter." in hero
    assert "Make the context it acts on provable." in hero
    assert (
        "Sidq verifies schema, lineage, governance, ownership, and documented "
        "claims before an agent acts."
    ) in hero
    assert hero.count('class="cta run"') == 1
    assert 'href="#decision">Watch Sidq block a risky change ↓</a>' in hero
    assert 'href="#install-connect">Run the proof ↓</a>' in hero
    assert "One command. A local graph." not in html
    assert "Try the proof yourself." in html
    assert "No setup maze." in html
    assert "Dependencies download on the first run" in html
    assert "needs no DataHub or account" in html
    assert "demo-stack" in html and "live-loop" in html and "DataHub" in html
    assert "starts DataHub" not in html
    assert 'class="rail"' not in html


def test_page_metadata_uses_the_judge_facing_positioning_and_owned_preview() -> None:
    html = _landing()

    assert "<title>Sidq — Provable Context for DataHub Agents</title>" in html
    assert (
        'property="og:title" content="Sidq — Provable Context for DataHub Agents"'
        in html
    )
    assert 'property="og:type" content="website"' in html
    assert 'property="og:url" content="https://sidq.mlki.app/"' in html
    assert (
        'property="og:image" content="https://sidq.mlki.app/social-preview.png"' in html
    )
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert (ROOT / "web" / "social-preview.png").is_file()


def test_footer_reads_release_identity_from_same_origin_without_html_injection() -> (
    None
):
    html = _landing()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'id="release-identity"' in html
    assert 'fetch("/healthz"' in script
    assert 'credentials: "same-origin"' in script
    assert "Deployed commit: ${release.commit_sha}" in script
    assert "Release: local/dev" in script
    assert "releaseIdentity.textContent" in script
    assert "/opt/sidq" not in html and "/opt/sidq" not in script


def test_fixture_commit_is_not_labelled_as_the_deployed_release() -> None:
    html = _landing()
    fixture_sha = "5addb753788935d4d1aa6a9483c28c6fc124e5c7"
    link = html.split(
        f'href="https://github.com/NexuChat/sidq/commit/{fixture_sha}"', 1
    )[0]

    assert link.endswith("Fixture evidence commit: <a ")
    assert "Deployed commit:" not in link[-100:]


def test_agent_copy_calls_latest_receipts_current_state_not_memory() -> None:
    html = _landing()
    agents = html.split('id="agents-title"', 1)[1].split("</section>", 1)[0]

    assert (
        "Spend a budget. Re-check shared current state. Work as a swarm. "
        "Refuse what you cannot prove."
    ) in agents
    assert '<span class="trace-type">Current state</span>' in agents
    assert "</strong> With explicit optional Receipt writes" in agents
    assert "Remember through the catalog" not in agents
    assert '<span class="trace-type">Memory</span>' not in agents


def test_receipt_handoff_and_mcp_tools_are_distinct_and_truthful() -> None:
    html = _landing()
    handoff = html.split('id="agent-handoff"', 1)[1].split("</section>", 1)[0]

    assert "A receipt is not authority." in handoff
    assert (
        "A separate reader re-reads the graph context and checks the receipt’s "
        "policy hash and age again."
    ) in handoff
    assert "A current PASS continues; BLOCK, missing, or stale stops." in handoff
    assert "An opted-in audit can write" not in handoff
    assert 'class="handoff-path"' not in handoff
    assert "<ol" not in handoff
    assert handoff.index('data-run="handoff"') < handoff.index('id="run-status"')
    assert handoff.index('id="run-output"') < handoff.index(
        '<details class="mcp-tools">'
    )
    tools = handoff.split('<details class="mcp-tools">', 1)[1].split("</details>", 1)[0]
    assert "three read-only mcp tools" in tools.lower()
    for tool in ("check_change", "verify_context", "search_verified"):
        assert f"<code>{tool}</code>" in tools
    assert "not a DataHub Receipt reader" in tools
    assert "The independent Receipt read is the separate live proof above." in tools
    assert 'data-run="handoff"' in handoff
    assert "check_change writes" not in handoff


def test_live_run_exposes_busy_state_to_assistive_technology() -> None:
    html = _landing()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'class="handoff-run" aria-busy="false"' in html
    assert 'runRegion.setAttribute("aria-busy", "true")' in script
    assert 'runRegion.setAttribute("aria-busy", "false")' in script


def test_general_trust_contract_precedes_the_concrete_example() -> None:
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
    assert 'class="trust-path"' in contract and 'tabindex="0"' not in contract
    for accessible_stage in (
        "Context: Schema, lineage, governance, and documented claims arrive from DataHub.",
        "Evidence check: Sidq cross-checks those claims",
        "Decision: PASS, WARN, or BLOCK.",
        "Receipt: An explicit audit can record the decision context",
        "Next agent: It re-checks the receipt for itself",
    ):
        assert f'aria-label="{accessible_stage}' in contract


def test_concrete_proof_separates_blocking_trigger_from_supporting_context() -> None:
    html = _landing()
    decision = html.split('id="decision"', 1)[1].split("</section>", 1)[0]

    assert "critical_downstream" in decision
    assert "Blocking trigger" in decision
    assert "wide_blast_radius" in decision
    assert "WARN" in decision
    assert "PII_Data" in decision
    assert "Sensitivity context" in decision
    assert "pii_exposure" not in decision
    assert "break quietly" not in decision


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
    assert "runOutput.focus()" in script
    assert "preventScroll" not in script


def test_mobile_hero_and_command_bidi_have_explicit_layout_guards() -> None:
    styles = STYLES.read_text(encoding="utf-8")
    mobile = styles[styles.rindex("@media (max-width: 780px)") :]

    assert re.search(r"\.hero\s*\{[^}]*padding:", mobile, flags=re.DOTALL)
    assert re.search(r"\.hero-note\s*\{[^}]*margin-top:", mobile, flags=re.DOTALL)
    assert re.search(
        r"\.trust-loop\s*\{[^}]*padding:\s*36px 0",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.trust-path\s*\{[^}]*grid-template-columns:\s*repeat\(6,",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.trust-path li:nth-child\(4\),\s*\.trust-path li:nth-child\(5\)\s*\{[^}]*grid-column:\s*span 3",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.trust-path p\s*\{[^}]*display:\s*none", mobile, flags=re.DOTALL
    )
    assert "overflow-x: auto" not in mobile
    assert re.search(
        r"\.verdict\s*\{[^}]*padding:\s*36px 0 44px",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.finding\s*\{[^}]*grid-template-columns:\s*1fr[^}]*padding:\s*12px 0",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.copy-command\s*\{[^}]*grid-template-columns:\s*1fr",
        mobile,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.cta\s*\{[^}]*white-space:\s*normal[^}]*text-align:\s*center",
        mobile,
        flags=re.DOTALL,
    )
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


def test_every_keyboard_interactive_control_has_the_shared_focus_ring() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert "a:focus-visible," in styles
    assert "button:focus-visible," in styles
    assert "summary:focus-visible" in styles
    assert "outline: 2px solid var(--acid)" in styles
    assert re.search(
        r"\.hero-source\s*\{[^}]*min-height:\s*24px",
        styles,
        flags=re.DOTALL,
    )


def test_reduced_motion_disables_smooth_scrolling_and_arrival_animation() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    reduce = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert re.search(r"html\s*\{[^}]*scroll-behavior:\s*auto", reduce, re.DOTALL)
    assert "@media (prefers-reduced-motion: no-preference)" in styles


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
