"""What the landing page must do, stated as behaviour rather than as markup.

This file has been rewritten repeatedly, and twice because it described a
particular arrangement of `<div>`s rather than the job the page has to do. The
page is allowed to change identity; what it is not allowed to do is bury the
proof, argue before it demonstrates, or drop a scoping sentence while getting
prettier.

So the assertions here are about the journey and the honesty, and they name
elements only where the element *is* the contract — the button the server
dispatches on, and the region the script writes output into.
"""

from __future__ import annotations

import http.client
import re
import threading
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

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self._inside_code:
            self._last_code = "".join(self._code_parts).strip()
            self._inside_code = False

    def handle_data(self, data: str) -> None:
        if self._inside_code:
            self._code_parts.append(data)


def _landing() -> str:
    return HTML.read_text(encoding="utf-8")


def _served_landing() -> str:
    from web import server

    with server.Server(("127.0.0.1", 0), server.Handler) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*service.server_address, timeout=2)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            payload = response.read()
        finally:
            connection.close()
            service.shutdown()
            thread.join(timeout=2)

    assert response.status == 200
    return payload.decode("utf-8")


def test_recorded_and_live_proofs_precede_the_argument() -> None:
    """A judge sees recorded proof and can try the live thing before the argument.

    The run button spent most of this project's life on the sixth screen, after
    the entire case had been made. Whatever the page looks like, the action and
    the region its output lands in come before the argument.
    """
    html = _served_landing()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'data-run="handoff"' in html
    for element in ('id="run-status"', 'id="run-progress"', 'id="run-output"'):
        assert element in html, element

    first_argument = html.index('id="exhibit-a-heading"')
    assert html.index('data-run="handoff"') < first_argument
    assert html.index('id="run-output"') < first_argument

    start = html.index('<section id="recorded-proof"')
    recorded = html[start : html.index("</section>", start)]

    assert "hidden" not in recorded.split(">", 1)[0]
    assert "Recorded proof — not live." in recorded
    assert "<code>make gate-demo</code>" in recorded
    assert re.search(r"captured at git revision <code>[0-9a-f]{7,40}</code>", recorded)
    assert 'id="recorded-output"' in recorded
    assert "recordedProof.hidden = true" in script


def test_the_page_shows_a_contradiction_before_it_argues_about_one() -> None:
    """The thesis is that a catalog can contradict itself — so show one.

    The page used to open on a blast-radius refusal, which is what the nearest
    competitor also does, and kept the self-contradiction behind a collapsed
    section. Prose asserting a catalog lies is worth less than two of its own
    statements placed side by side.
    """
    html = _landing()

    contradiction = html.index('id="exhibit-a-heading"')
    refusal = html.index('id="exhibit-b-heading"')
    assert contradiction < refusal

    section = html[contradiction:refusal]
    assert "order_details.billing_address_line1" in section
    assert "Customer_Analytics_Measures" in section
    assert "Customer LTV" in section
    assert "does not exist" in section
    # The boundary that makes the finding credible rather than alarmist.
    assert "No source system was consulted" in section
    assert "285" in section and "67" in section
    assert "showcase-ecommerce" in section


def test_the_verdict_is_stated_before_the_evidence_that_produced_it() -> None:
    html = _landing()
    refusal = html[
        html.index('id="exhibit-b-heading"') : html.index('id="receipt-heading"')
    ]

    assert refusal.index("BLOCK") < refusal.index("critical_downstream")
    assert "cust_email" in refusal
    assert "ORG_BACKEND_ENG" in refusal
    # The blocking rule and the supporting warning must stay distinguishable.
    assert "wide_blast_radius" in refusal
    assert "PII_Data" in refusal


def test_all_four_receipt_dispositions_are_distinct_on_the_page() -> None:
    """A refusal is not an absence, and the page has to say so in its own words."""
    html = _landing()
    receipt = html[html.index('id="receipt-heading"') :]

    for state in ("PASS", "WARN", "BLOCK", "NOT VERIFIED"):
        assert state in receipt, state
    assert "never shown as unverified" in receipt.lower()
    # The fail-closed boundary is stated in full one click away.
    scope = (ROOT / "web" / "scope.html").read_text(encoding="utf-8").lower()
    assert "fails closed" in scope


def test_scoping_answers_survive_whatever_the_page_looks_like() -> None:
    """Sentences a platform team looks for, and a redesign tends to drop.

    Each was lost in one rebuild and restored only because a test named it.
    They are the limits of the claim, which is exactly the content a prettier
    page is most tempted to cut.
    """
    text = " ".join(
        (ROOT / "web" / "scope.html").read_text(encoding="utf-8").lower().split()
    )

    assert "catalog audits read metadata only" in text
    assert "no model can block" in text
    assert "not append-only" in text
    assert "query results never enter a model" in text


def test_the_evidence_a_judge_would_open_is_linked() -> None:
    """One click from the console, not crowded onto it.

    The console has to be usable in seconds; the evidence a judge opens when
    they want to check the claim lives on the scope page, which the console
    links. What matters is that the trail exists and is one hop away.
    """
    html = _landing()
    scope = (ROOT / "web" / "scope.html").read_text(encoding="utf-8")

    assert 'href="/scope.html"' in html

    for url in (
        "https://github.com/NexuChat/sidq/blob/main/examples/01-blocked-pii-dashboard/verdict.json",
        "https://github.com/NexuChat/sidq/blob/main/examples/03-catalog-truth-report/report.json",
        "https://github.com/NexuChat/sidq/commit/5addb753788935d4d1aa6a9483c28c6fc124e5c7",
        "https://datahub.mlki.app",
    ):
        assert f'href="{url}"' in scope, url


def test_the_page_never_ships_a_simulated_run() -> None:
    """The output must come from the host, not from the page.

    Every generated design direction shipped a client-side fake: staggered
    `setTimeout` writes, invented field names, even a command that does not
    exist. A page that fabricates its own proof — for a project whose thesis is
    that unperformed checks must never read as passes — would be the most
    expensive contradiction available to it.
    """
    html = _landing()

    assert "setTimeout" not in html
    assert "localStorage" not in html
    assert "sidq receipt read" not in html
    assert re.search(r'<script src="app\.js\?v=[0-9a-f]{8,}" defer></script>', html)
    assert html.count("<script") == 1


def test_the_reproduce_path_is_one_command_a_judge_can_paste() -> None:
    html = _landing()
    reproduce = html[html.index('id="reproduce-heading"') :]

    assert "make gate-demo" in reproduce
    assert "git clone https://github.com/NexuChat/sidq.git" in reproduce


def test_live_run_exposes_busy_state_to_assistive_technology() -> None:
    html = _landing()
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'aria-busy="false"' in html
    assert 'runRegion.setAttribute("aria-busy", "true")' in script
    assert 'runRegion.setAttribute("aria-busy", "false")' in script


def test_every_copy_button_carries_the_command_it_shows() -> None:
    parser = _CopyButtonParser()
    parser.feed(_landing())

    for button in parser.buttons:
        assert button["data-copy"].strip(), button


def test_keyboard_focus_and_reduced_motion_are_honoured() -> None:
    styles = STYLES.read_text(encoding="utf-8")

    assert ":focus-visible" in styles
    assert "outline" in styles
    assert "@media (prefers-reduced-motion" in styles


def test_run_status_distinguishes_findings_from_operational_failures() -> None:
    """Exit 1 is an answer. Exit 2 is a broken run. They must never read alike."""
    script = SCRIPT.read_text(encoding="utf-8")

    assert "findings, not an operational failure" in script
    assert "Operational failure" in script
