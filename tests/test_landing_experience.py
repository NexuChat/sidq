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


def test_landing_leads_with_the_contradiction_and_offers_the_run_immediately() -> None:
    """The page must argue in the order that convinces, and let a judge try it first.

    It used to open on an abstract five-step trust diagram, then a blast-radius
    refusal — which is the one thing the nearest competitor also does — and it
    buried the run button six screens down. The differentiator is that the
    catalog contradicts *itself*, so that is what leads, and the live proof is
    reachable before any of the argument.
    """
    html = _landing()
    hero = html.split('<section class="hero', 1)[1].split("</section>", 1)[0]

    for anchor in (
        'id="contradiction"',
        'id="decision"',
        'id="agent-handoff"',
        'id="install-connect"',
    ):
        assert anchor in html, anchor

    # The run action lives in the hero, and its output lands directly beneath.
    assert 'data-run="handoff"' in hero
    assert html.index('data-run="handoff"') < html.index('id="contradiction"')
    assert html.index('id="run-output"') < html.index('id="contradiction"')

    assert html.index('id="contradiction"') < html.index('id="decision"')
    assert html.index('id="decision"') < html.index('id="agent-handoff"')
    assert html.index('id="agent-handoff"') < html.index('id="install-connect"')
    assert html.index('id="install-connect"') < html.index('class="deep-dives')

    # The abstract contract section restated what the concrete sections prove.
    assert 'id="trust-loop"' not in html

    numbers = [
        int(match.group(1))
        for match in re.finditer(r'class="(?:eyebrow|kicker)">(\d+) / ', html)
    ]
    assert numbers == sorted(numbers), numbers
    assert len(numbers) == len(set(numbers)), numbers

    assert "DataHub-native trust infrastructure" in hero
    assert "Everyone is teaching agents to read the catalog." in hero
    assert "Nobody is asking whether it is lying." in hero
    assert "Try the proof yourself." not in html


def test_the_contradiction_shows_two_catalog_claims_that_cannot_both_hold() -> None:
    """The thesis is that a catalog lies. The page has to *show* one.

    Prose asserting it is worth less than the two statements side by side, and
    the section must keep the boundary that makes the finding credible: catalog
    metadata only, no source system consulted.
    """
    html = _landing()
    section = html.split('id="contradiction"', 1)[1].split("</section>", 1)[0]

    assert "order_details.billing_address_line1" in section
    assert "Customer_Analytics_Measures" in section
    assert "Customer LTV" in section
    assert "does not exist" in section
    assert "No source system was consulted" in section
    assert "285" in section and "67" in section
    assert "showcase-ecommerce" in section


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
    assert "shared catalog state" in agents.lower() or "shared current state" in agents
    assert "at-least-once, never exactly-once" in agents
    assert "no coordinator" in agents


def test_receipt_handoff_and_mcp_tools_are_distinct_and_truthful() -> None:
    html = _landing()
    handoff = html.split('id="agent-handoff"', 1)[1].split("</section>", 1)[0]

    assert "A receipt is not authority." in handoff
    assert (
        "A separate reader re-reads the graph context and checks the receipt’s "
        "policy hash and age again"
    ) in handoff
    # All four dispositions, stated separately. A page that folded a refusal in
    # with "not verified" would misdescribe the one behaviour the demo exists to
    # show — and the four lines are what a judge reads to see they are distinct.
    assert "<strong>Current PASS</strong> → continue." in handoff
    assert "<strong>Current WARN</strong> → review or escalate." in handoff
    assert "<strong>Current BLOCK</strong> → stop." in handoff
    assert "never shown as unverified" in handoff
    assert (
        "<strong>Missing, stale, or unreadable</strong> → <code>NOT VERIFIED</code>"
    ) in handoff
    assert "An opted-in audit can write" not in handoff
    assert 'class="handoff-path"' not in handoff
    assert "<ol" not in handoff
    tools = handoff.split('<details class="mcp-tools">', 1)[1].split("</details>", 1)[0]
    assert "three read-only mcp tools" in tools.lower()
    for tool in ("check_change", "verify_context", "search_verified"):
        assert f"<code>{tool}</code>" in tools
    assert "not a DataHub Receipt reader" in tools
    assert "The independent Receipt read is the separate live proof above." in tools
    assert "check_change writes" not in handoff


def test_live_run_exposes_busy_state_to_assistive_technology() -> None:
    html = _landing()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'class="handoff-run" aria-busy="false"' in html
    assert 'runRegion.setAttribute("aria-busy", "true")' in script
    assert 'runRegion.setAttribute("aria-busy", "false")' in script


def test_concrete_proof_separates_blocking_trigger_from_supporting_context() -> None:
    html = _landing()
    decision = html.split('id="decision"', 1)[1].split("</section>", 1)[0]

    assert "critical_downstream" in decision
    assert "Blocking rule" in decision
    assert "wide_blast_radius" in decision
    assert "WARN" in decision
    assert "PII_Data" in decision
    assert "Sensitivity context" in decision
    assert "pii_exposure" not in decision
    assert "break quietly" not in decision


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


def test_narrow_phone_compacts_the_proof_before_the_two_screen_boundary() -> None:
    styles = STYLES.read_text(encoding="utf-8")
    narrow = styles[styles.rindex("@media (max-width: 340px)") :]

    assert re.search(r"\.masthead\s*\{[^}]*min-height:\s*52px", narrow, flags=re.DOTALL)
    assert re.search(
        r"\.trust-path li,\s*\.trust-path li:last-child\s*\{[^}]*min-height:\s*52px",
        narrow,
        flags=re.DOTALL,
    )
    assert re.search(
        r"\.verdict\s*\{[^}]*padding:\s*20px 0 32px",
        narrow,
        flags=re.DOTALL,
    )
    assert re.search(r"\.verdict-head\s*\{[^}]*gap:\s*12px", narrow, flags=re.DOTALL)
    assert re.search(r"\.finding\s*\{[^}]*padding:\s*8px 0", narrow, flags=re.DOTALL)


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

    assert install.count('class="copy-command"') >= 1
    assert "Replay the proof." in install
    assert "Connect Codex + DataHub" in install
    assert install.count("<details>") == 1
    assert "<details open" not in install
    assert install.index("Replay the proof.") < install.index("<details>")
    # The offline replay is the one thing in the open; everything a connected
    # run needs is one click away rather than three panels of manual.
    assert install.index("make gate-demo") < install.index("<details>")
    assert "make live-loop" in install
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
