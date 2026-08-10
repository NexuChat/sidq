"""Every number the README claims must trace to a committed evidence file.

The project requires that "every number in the README traces to
a file in the repo — no unsupported claim". That was verified by hand, which means
it holds only until the next edit. These tests mechanise it, so a headline number
that drifts from its evidence breaks the build instead of reaching a judge.

The catalog audit and the reconciliation example both need a live DataHub, so
their artifacts cannot be regenerated inside a unit test. What can be checked
without any network is that the prose and the committed artifact still agree.
"""

from __future__ import annotations

import gzip
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import eval_preflight

ROOT = Path(__file__).parents[1]

# The pre-flight corpus is 32 MB of labelled mutations and is deliberately not
# committed — it is regenerable from `scripts/generate_mutations.py` and would
# dominate a repository whose whole point is being cheap to clone and verify.
#
# The four guards below read it, so on a fresh clone they must SKIP rather than
# pass. That distinction is the project's own rule applied to its own test
# suite: a check that could not run is not a check that succeeded. Silently
# passing them would have been the more convenient lie, and `make check` — which
# the README hands to a judge — would have reported four green results for
# assertions nothing verified.
_CORPUS = ROOT / "data" / "benchmark" / "labelled-regression.jsonl.gz"
README = ROOT / "README.md"
TRUTH_REPORT = ROOT / "examples" / "03-catalog-truth-report" / "report.json"


def _regression_rows() -> list[dict]:
    with gzip.open(_CORPUS, mode="rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _summary() -> dict[str, dict[str, int]]:
    raw = json.loads(TRUTH_REPORT.read_text(encoding="utf-8"))
    return {entry["check"]: entry for entry in raw["summary"]}


def test_the_headline_contradiction_count_matches_the_evidence() -> None:
    """The README's first claim must name what was examined and what was found.

    It used to say "285 contradictions across 67 datasets", and that guard
    enforced the phrasing — but 67 is the number examined, not the number
    affected, and a reader hears "spread over 67". Hand-checking the evidence
    against the live catalog is what caught it. Both numbers still have to
    appear; what changed is that they can no longer be conflated.
    """
    entry = _summary()["lineage_field_missing"]
    text = README.read_text(encoding="utf-8")

    assert f"{entry['findings']} internal contradictions" in text
    assert f"**{entry['datasets_examined']} datasets**" in text
    assert "contradictions across 67 datasets" not in text


def test_no_judge_facing_surface_conflates_examined_with_affected() -> None:
    """The conflation was banned in the README, then published in a picture.

    The guard above reads one file. The submission gallery is composed as HTML
    and rendered to PNG, and those boards say the same numbers to the same
    judges — so the phrasing the README may not use, a board may not use
    either. This was not hypothetical: `docs/gallery/src/02-the-sample.html`
    carried "internal contradictions across 67 datasets" while the README was
    forbidden from saying it, and a rendered slide is invisible to any check
    that only reads prose.

    The boards are the reason this scans sources rather than images. Nothing
    here can read a PNG; keeping the wording honest in the HTML the PNG is
    rendered from is what keeps the rendered board honest.

    Markup is stripped before matching, and that is the whole difficulty. The
    offending board read `across <strong>67 datasets</strong>`, so the banned
    phrase never appeared literally in the file — a substring check against raw
    HTML reports clean on the exact text it exists to forbid. A reader sees the
    sentence; only a reader that strips tags sees it too.
    """
    surfaces = [
        *ROOT.glob("*.md"),
        *(ROOT / "docs").glob("*.md"),
        *(ROOT / "docs" / "gallery" / "src").glob("*.html"),
        *(ROOT / "web").glob("*.html"),
    ]
    assert surfaces, "no judge-facing surfaces were found to check"

    offenders = []
    for path in surfaces:
        raw = path.read_text(encoding="utf-8")
        rendered = re.sub(r"<[^>]+>", "", raw) if path.suffix == ".html" else raw
        if "contradictions across 67 datasets" in " ".join(rendered.split()):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert not offenders, (
        "67 is the number of datasets examined, not the number affected; "
        f"these surfaces conflate the two: {offenders}"
    )


def test_the_unowned_asset_count_matches_the_evidence() -> None:
    entry = _summary()["unowned_consumed"]

    assert f"{entry['findings']} consumed-but-unowned assets" in README.read_text(
        encoding="utf-8"
    )


def test_the_powerbi_example_number_is_the_one_the_evidence_holds() -> None:
    """The README singles out one asset. Its number must be countable in the file.

    It used to claim the asset "carries 58 column-lineage edges, 57 of which
    target fields that do not exist". The 57 is one committed finding per edge
    and a reader can count them. The 58 was a total-edge figure for that asset
    that no committed artifact records — `report.json` stores the findings, and
    its `scope.lineage_edges` counts the whole catalog, not one asset. A number
    a judge cannot trace is the exact defect this project exists to catch, so
    the total was dropped rather than evidenced from a live run nobody can
    repeat. This guard pins the surviving claim to the evidence and keeps the
    unprovable one from coming back.
    """
    raw = json.loads(TRUTH_REPORT.read_text(encoding="utf-8"))
    # Normalised: a guard a markdown reflow can silence is not a guard, and the
    # retired "58" could otherwise walk back in across a line break.
    text = " ".join(README.read_text(encoding="utf-8").split())
    subject = "Customer_Analytics_Measures"

    findings = [
        item
        for item in raw["evidence"]
        if item["kind"] == "lineage_field_missing" and subject in item["subject"]
    ]
    assert findings, f"{subject} must still appear in the audit evidence"

    # The README says *edges*; the evidence file stores *findings*. Those are
    # the same number only while each finding carries a distinct edge, so pin
    # that rather than trusting today's data to keep the two words honest.
    edges = {json.dumps(item["detail"]["edge"], sort_keys=True) for item in findings}
    assert len(edges) == len(findings), (
        f"{len(findings)} findings collapse to {len(edges)} distinct edges; "
        "the README may no longer describe them as edges"
    )

    claimed = {
        int(value)
        for value in re.findall(
            r"\*\*(\d+) column-lineage edges into fields that schema does not", text
        )
    }
    assert claimed, "the README must still quote the missing-target edge count"
    # Every claimed value, not just the largest: a wrong second figure sitting
    # beside the right one is still a wrong figure on a judge-facing page.
    assert claimed == {len(edges)}, (
        f"README claims {sorted(claimed)} contradicted edges for {subject}; "
        f"the evidence file has {len(edges)}"
    )
    assert "58 column-lineage edges" not in text, (
        "the per-asset total-edge count has no committed evidence; state only "
        "the contradicted edges, which report.json records one by one"
    )


def test_the_unverifiable_negative_result_is_still_honest() -> None:
    """The README's strongest honesty signal: 32/32 lineage_rot unverifiable."""
    text = README.read_text(encoding="utf-8")

    assert "32/32" in text
    assert "unverifiable" in text


# ---------------------------------------------------------------------------
# The pre-flight negative result. PREFLIGHT.md §6 pre-commits to publishing it.
# ---------------------------------------------------------------------------


def test_the_preflight_result_matches_the_corpus_it_reports_on() -> None:
    """A published negative result is still a claim, and must stay true."""
    assert eval_preflight.render(
        eval_preflight.evaluate(_regression_rows())
    ) == eval_preflight.DOCUMENT.read_text(encoding="utf-8"), (
        "docs/PREFLIGHT-RESULTS.md is stale; rerun scripts/eval_preflight.py"
    )


def test_the_corpus_input_contract_has_no_verdict_leak() -> None:
    """§3's leak rule, enforced mechanically rather than by reviewer vigilance.

    A feature built from the verdict would score beautifully and be worthless —
    the failure mode §3 calls hardest to notice after the fact.
    """
    result = eval_preflight.evaluate(_regression_rows())

    assert result["leaked_keys"] == []
    assert result["unexpected_keys"] == []


def test_preflight_is_not_advertised_as_shipped_while_it_is_not() -> None:
    """The spec is a binding contract; it must not read as delivered capability."""
    spec = (ROOT / "docs" / "PREFLIGHT.md").read_text(encoding="utf-8")
    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")

    assert "not shipped" in spec.lower()
    assert "not shipped" in results.lower()


def test_a_non_model_rung_still_ties_the_best_model() -> None:
    """The published conclusion rests on this, so it is asserted, not narrated.

    This assertion has been rewritten three times, each time because it fired when
    the facts moved: when the corpus gained label variance, when deterministic
    pre-checks took the false-negative rate under the bar, and when a two-term rule
    turned out to tie the classifiers. That is the guard doing its job — a
    published conclusion has to keep matching its own evidence.
    """
    rungs = eval_preflight.load_rungs()
    assert rungs is not None, "run scripts/train_preflight.py"
    by_name = {rung["rung"]: rung for rung in rungs["rungs"]}

    rule = next(rung for name, rung in by_name.items() if name.startswith("L0.75"))
    models = [
        rung
        for name, rung in by_name.items()
        if name.startswith(("L1", "L2")) and rung["abstention_rate"] <= 0.5
    ]
    assert models, "the ladder must publish trained rungs to compare against"
    best_model = min(models, key=lambda rung: rung["false_negative_rate"])

    assert rule["false_negative_rate"] <= best_model["false_negative_rate"], (
        "a trained rung now beats the deterministic rule; §1's first condition may "
        "hold after all and both documents must be rewritten before the "
        "no-model-needed conclusion can stand"
    )
    assert rule["false_positives"] <= best_model["false_positives"]

    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")
    assert "no model is needed" in results
    assert "cannot tell us whether pre-flight is hard" in results


def test_the_published_deliverable_is_the_cheapest_rung_that_meets_the_bar() -> None:
    """§4: cheapest, not most sophisticated. A tie must resolve downward."""
    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")

    assert "The cheapest rung that meets the bar is **L0.75" in results, (
        "the report must name the rule, not a classifier that merely ties it"
    )


def test_the_ladder_was_split_by_model_not_by_row() -> None:
    """§3: a row-wise split measures memorisation and reports a useless number."""
    rungs = eval_preflight.load_rungs()
    assert rungs is not None

    assert rungs["holdout_models"], "the held-out models must be named"
    assert rungs["train_rows"] and rungs["test_rows"]
    # Every held-out name is a dbt model path, not a row id.
    assert all(name.endswith(".sql") for name in rungs["holdout_models"])


def test_the_readme_audit_section_agrees_with_the_published_evidence() -> None:
    """The one-command pitch must quote the guarded number, not a live-run one.

    The README now describes a live `sidq audit` run beside the recorded audit,
    and the two legitimately differ — the live catalog carries the demo project as
    well as the sample. The contradiction count is identical in both and is the
    figure the pitch leans on, so that one is pinned here; the unowned count is
    explained in prose rather than quoted as a headline, because a number that
    moves with catalog contents cannot be a claim.
    """
    entry = _summary()["lineage_field_missing"]
    text = README.read_text(encoding="utf-8")

    assert f"all {entry['findings']} `lineage_field_missing` contradictions" in text
    # The scope difference must stay explained, not quietly dropped.
    assert "Same check, different catalog contents." in text


def test_the_readme_leads_with_something_a_judge_can_run() -> None:
    """The first actionable thing must be a command, not an installation."""
    text = README.read_text(encoding="utf-8")

    assert "## Try it in one command" in text
    assert "sidq audit" in text
    # Writing to someone else's catalog must never read as the default.
    assert "It is off by\ndefault" in text or "off by default" in text


# ---------------------------------------------------------------------------
# Commands we tell people to run must exist. Checking the landing page's numbers
# while never checking its instructions is how `make gate-demo` reached a judge-
# facing surface without ever being a target.
# ---------------------------------------------------------------------------

# A judge surface is any page a judge is sent to, not the front door
# specifically. Pinning the scoping sentences to `web/index.html` forced 676
# words of caveat onto the one screen that has to be usable in seconds — and
# every attempt to thin the page put them straight back. They belong on a
# surface a reader reaches in one click, stated in full, rather than crowding
# the page that has to convince in ten seconds.
SCOPE_SURFACE = "web/scope.html"

JUDGE_FACING = ("README.md", SCOPE_SURFACE, "docs/SETUP.md", "docs/DEVPOST.md")


def _make_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-zA-Z][\w-]*):", text, flags=re.MULTILINE))


@pytest.mark.parametrize("document", JUDGE_FACING)
def test_every_make_command_we_publish_exists(document: str) -> None:
    """A published instruction that fails is worse than no instruction."""
    text = (ROOT / document).read_text(encoding="utf-8")
    # A sentence such as the approved tagline "make the context provable" is not
    # a shell instruction. Commands are published as standalone lines, inline
    # code, HTML code, or a fixed `data-command` value.
    referenced = set(
        re.findall(
            r"(?m)(?:^|`|<code>|data-command=\")make ([a-z][\w-]*)",
            text,
        )
    )
    missing = sorted(referenced - _make_targets())

    assert not missing, f"{document} tells a reader to run: {', '.join(missing)}"


def test_the_landing_page_command_is_the_one_that_reproduces_the_verdict() -> None:
    """The page promises 'the same deterministic verdict'; the target must deliver it."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "gate-demo:" in makefile
    body = makefile.split("gate-demo:", 1)[1].split("\ndemo-up:", 1)[0]
    assert "regenerate_example_01.py --check" in body, (
        "gate-demo must verify the published verdict is current, not just print it"
    )


def test_the_prior_work_disclosure_matches_what_is_shipped() -> None:
    """The rules require disclosing pre-existing work, and we ship mined corpora.

    `docs/DEVPOST.md` previously said no pre-existing code was incorporated while
    `data/claims/` shipped material derived from SchemaStore, FHIR and dozens of
    dbt repositories. Sidq's own source is original; the data is not, and a
    submission field that says otherwise is a rules problem, not a wording one.
    """
    text = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")

    assert "Third-party material is included" in text
    for source in ("SchemaStore", "FHIR", "showcase-ecommerce"):
        assert source in text, f"the disclosure must name {source}"
    assert "ATTRIBUTION.md" in text


def test_the_published_reader_scores_are_the_ones_the_artifact_records() -> None:
    """The one measured ML claim in the project must come from its own evidence.

    95.8% precision was quoted in three judge-facing documents and tied to
    nothing: retraining the head would have changed `report.json` and left the
    prose stating an old number, which is the drift every other published
    figure here is guarded against.
    """
    report = json.loads(
        (ROOT / "data/claims/reader/report.json").read_text(encoding="utf-8")
    )
    operating = report["operating_point"]
    quoted = {
        "precision": f"{operating['precision'] * 100:.1f}%",
        "recall": f"{operating['recall'] * 100:.1f}%",
        "baseline": f"{report['rule_baseline']['precision'] * 100:.1f}%",
        "proposals": str(int(operating["proposals"])),
    }

    for name in ("README.md", "docs/CLAIM-READER.md", "docs/CLAIMS-MATRIX.md"):
        text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
        for label in ("precision", "recall"):
            assert quoted[label] in text, (
                f"{name} does not state the recorded {label} "
                f"{quoted[label]} from data/claims/reader/report.json"
            )

    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    assert f"{quoted['proposals']} proposals" in readme
    # The baseline is what makes the headline number mean anything; a document
    # that drops it is quoting a score with nothing to compare it against.
    matrix = " ".join(
        (ROOT / "docs/CLAIMS-MATRIX.md").read_text(encoding="utf-8").split()
    )
    assert quoted["baseline"] in matrix


def test_the_test_count_in_the_judge_runbook_is_the_real_one() -> None:
    """The runbook tells a judge how many tests `make check` runs; it must be true.

    A stale number is a small lie in the first table a judge reads, on the one
    page whose whole argument is that published claims are checked. Collected
    rather than run, so this stays fast and cannot recurse into itself.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"(\d[\d,]*) tests, lint, format, types", text)
    assert match, "the judge runbook no longer states a test count"

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    collected = re.search(r"(\d+) tests? collected", completed.stdout)
    assert collected, completed.stdout[-400:]

    published = int(match.group(1).replace(",", ""))
    actual = int(collected.group(1))
    assert published == actual, (
        f"README says {published} tests; pytest collects {actual}. "
        "Update the runbook table."
    )

    outcome = re.search(
        r"(\d[\d,]*) passed, (\d[\d,]*) optional integrations? skipped, "
        r"with ([\d.]+)% branch coverage",
        text,
    )
    assert outcome, "the judge runbook no longer states pass, skip, and coverage data"
    passed = int(outcome.group(1).replace(",", ""))
    skipped = int(outcome.group(2).replace(",", ""))
    coverage = outcome.group(3)
    assert passed + skipped == actual

    qa = (ROOT / "docs/QA-RESULTS.md").read_text(encoding="utf-8")
    claims = (ROOT / "docs/CLAIMS-MATRIX.md").read_text(encoding="utf-8")
    audit = (ROOT / "docs/SECURITY-AUDIT.md").read_text(encoding="utf-8")
    normalized_qa = " ".join(qa.split())
    normalized_claims = " ".join(claims.split())
    normalized_audit = " ".join(audit.split())
    assert f"{passed} passed" in normalized_qa
    # One skip reads "1 optional integration test skipped"; two read "…tests…".
    # The count is the claim; the grammar around it must be allowed to agree.
    assert re.search(rf"{skipped} optional integration tests? skipped", normalized_qa)
    assert f"{passed} pass" in normalized_claims
    assert re.search(rf"{skipped} optional integrations? skips?", normalized_claims)
    assert f"{passed} passed, {skipped} skipped" in normalized_audit
    for evidence in (qa, claims, audit):
        assert f"{coverage}%" in evidence

    # Any judge-facing document that quotes a suite size must quote this one.
    # The rubric table in ARCHITECTURE.md was the first to restate it outside
    # the runbook, and nothing would have caught it drifting.
    for name in ("ARCHITECTURE.md", "README.md", "docs/CLAIMS-MATRIX.md"):
        document = (ROOT / name).read_text(encoding="utf-8")
        for quoted in re.findall(r"([\d,]{3,}) tests\b", document):
            assert int(quoted.replace(",", "")) == actual, (
                f"{name} says {quoted} tests; pytest collects {actual}"
            )


def test_the_landing_page_can_only_run_read_only_commands() -> None:
    """The page runs commands on the host, so what it *can* run is a guarded set.

    The safety of that endpoint is not the absence of a bug — it is that the table
    contains nothing which writes. This asserts the property directly, so adding a
    mutating entry breaks the build instead of quietly shipping a public write.
    """
    from web import server

    RUNNABLE = server.RUNNABLE

    for name, (_, argv) in RUNNABLE.items():
        joined = " ".join(argv)
        # The flags that make a sidq command mutate a catalog. A bare `repair`
        # is a dry run by construction — the CLI writes only under --apply, and
        # tests/test_repair_agent.py pins that a dry run performs zero tool
        # calls — so the guard names the write paths, not the word.
        # --write-assertions is refused without --write-receipts, so it is
        # named here too rather than left to that dependency.
        assert not {"--apply", "--write-receipts", "--write-assertions"} & set(argv), (
            f"{name} would write to the catalog: {joined}"
        )
        # Scripts that rewrite artifacts or reset the demo state.
        assert "reset" not in joined, f"{name} mutates demo state: {joined}"
        if "regen" in joined:
            # `regen --check` verifies; bare `regen` rewrites committed files.
            assert "--check" in argv, f"{name} rewrites artifacts: {joined}"


def test_a_runaway_command_cannot_return_unbounded_output(monkeypatch) -> None:
    """The endpoint is public, so its response size is bounded by the server."""
    from web import server

    class Endless:
        returncode = 0

        def communicate(self, timeout):
            assert timeout == server.TIMEOUT_SECONDS
            return "x" * (server.MAX_OUTPUT_BYTES + 1), ""

    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: Endless())

    output = server._run("gate-demo")["output"]

    assert isinstance(output, str)
    assert output.endswith(server.TRUNCATION_MARKER)
    assert len(output.encode("utf-8")) <= server.MAX_OUTPUT_BYTES


def _stub_handler(server, monkeypatch, responses, path: str):
    handler = server.Handler.__new__(server.Handler)
    handler.path = path
    handler.client_address = ("192.0.2.1", 12345)
    handler.headers = {
        "Host": "sidq.mlki.app",
        "Origin": "https://sidq.mlki.app",
        "Sec-Fetch-Site": "same-origin",
        server.DEMO_REQUEST_HEADER: server.DEMO_REQUEST_HEADER_VALUE,
        server.CAPABILITY_HEADER: server._issue_capability(
            None, path.removeprefix("/run/")
        )[0],
    }
    monkeypatch.setattr(
        handler, "_json", lambda status, payload: responses.append((status, payload))
    )
    return handler


def test_replaying_the_same_command_is_refused_with_a_retry_hint(monkeypatch) -> None:
    from web import server

    responses: list = []
    monkeypatch.setattr(server, "_run", lambda name: {"command": name})
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    server._last_run_finished.clear()

    handler = _stub_handler(server, monkeypatch, responses, "/run/gate-demo")
    handler.do_POST()
    handler.headers[server.CAPABILITY_HEADER] = server._issue_capability(
        None, "gate-demo"
    )[0]
    handler.do_POST()
    server._last_run_finished.clear()

    assert responses[0][0] == 200
    assert responses[1] == (
        429,
        {"error": "run cooldown active — try again later", "retry_after": 30},
    )


def test_the_cooldown_does_not_punish_a_reader_trying_the_next_button(
    monkeypatch,
) -> None:
    """The page offers three buttons and a reader clicks them in sequence.

    A per-client cooldown would make the second click fail, which costs a judge
    the demonstration and buys nothing: `_lock` already permits one run at a
    time regardless of who asks. The key is (client, command) for that reason,
    and the first version of this keyed on the client alone.
    """
    from web import server

    responses: list = []
    monkeypatch.setattr(server, "_run", lambda name: {"command": name})
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0)
    server._last_run_finished.clear()

    _stub_handler(server, monkeypatch, responses, "/run/gate-demo").do_POST()
    _stub_handler(server, monkeypatch, responses, "/run/repair").do_POST()
    server._last_run_finished.clear()

    assert [status for status, _ in responses] == [200, 200]


def test_the_landing_page_run_buttons_name_commands_that_exist() -> None:
    """A button wired to a name outside the table is a 404 in a judge's face."""
    from web.server import RUNNABLE

    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    wired = set(re.findall(r'data-run="([\w-]+)"', html))

    assert wired, "the landing page no longer offers a runnable command"
    assert wired <= set(RUNNABLE), (
        f"unknown run targets: {sorted(wired - set(RUNNABLE))}"
    )
    assert {"handoff", "claims"} <= wired, (
        "the live page must expose the receipt handoff and measured-doc claims, "
        "not only the older gate/audit/repair paths"
    )


def test_the_live_handoff_is_a_fixed_read_only_receipt_read() -> None:
    """The winning demo reads shared memory without granting public writes."""
    from web.server import RUNNABLE

    _, argv = RUNNABLE["handoff"]
    assert argv[1:3] == (
        "verify",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)",
    )
    assert "--write-receipts" not in argv


def test_the_public_server_declares_a_hardened_browser_boundary() -> None:
    from web.server import SECURITY_HEADERS

    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]
    assert "form-action 'none'" in SECURITY_HEADERS["Content-Security-Policy"]
    assert "camera=()" in SECURITY_HEADERS["Permissions-Policy"]


def test_the_published_skill_example_matches_the_verdict_artifact() -> None:
    """A copy-pasted skill example must not disagree with its evidence file."""
    verdict = json.loads(
        (ROOT / "examples/01-blocked-pii-dashboard/verdict.json").read_text()
    )
    skill = (ROOT / "skills/datahub-verify/SKILL.md").read_text()

    assert f'"commit_sha": "{verdict["commit_sha"]}"' in skill
    assert f'"policy_hash": "{verdict["policy_hash"]}"' in skill
    assert "npx skills add NexuChat/sidq --skill datahub-verify" in skill


def test_the_service_unit_enforces_the_runtime_boundary() -> None:
    """The committed deployment unit is the reproducible production contract."""
    unit = (ROOT / "deploy/sidq-landing.service").read_text()

    for directive in (
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "CapabilityBoundingSet=",
        "MemoryMax=2G",
        "CPUQuota=200%",
    ):
        assert directive in unit
    assert "ExecStart=" in unit and "web/server.py" in unit
    assert "LoadCredential=datahub-reader-token:/etc/sidq/datahub-reader.token" in unit
    assert "EnvironmentFile=" not in unit


def test_the_operations_runbook_covers_probe_release_and_rollback() -> None:
    runbook = (ROOT / "docs/OPERATIONS.md").read_text()

    for required in (
        "/healthz",
        "/readyz",
        "systemctl restart sidq-landing",
        "/opt/sidq/releases/<SHA>",
        "/opt/sidq/current",
        'sudo ln -s "releases/$release_sha"',
        'sudo ln -s "releases/$rollback_sha"',
        "mv -Tf",
        ".sidq-dev-lock",
        "git rev-parse HEAD",
        "git status --porcelain",
        'git archive "$release_sha"',
        "chown -R root:root",
        "chmod -R a-w,a+rX",
        "curl --fail",
    ):
        assert required in runbook

    release = runbook.split("## Release", 1)[1].split("## Configuration and logs", 1)[0]
    rollback = runbook.split("## Rollback", 1)[1]
    for procedure in (release, rollback):
        switch_at = procedure.index("sudo ln -s")
        before_restart, restart, _ = procedure.partition(
            "systemctl restart sidq-landing"
        )
        assert restart
        assert procedure.rindex("cmp --silent") < switch_at < len(before_restart)
        assert "ln -sfn" not in procedure
        assert "touch /opt/sidq/runtime/venv/.sidq-dev-lock" not in procedure
        assert "-nt /opt/sidq/current/" not in procedure
        for prerequisite in ("requirements-dev.lock", "pyproject.toml", "uv.lock"):
            assert prerequisite in procedure[:switch_at]
            assert f"/opt/sidq/runtime/{prerequisite}" in procedure[:switch_at]
        assert "STOP" in procedure[:switch_at]
        assert "docs/SETUP.md" in procedure[:switch_at]

    normalized_release = " ".join(release.replace("\\\n", "").split())
    normalized_rollback = " ".join(rollback.replace("\\\n", "").split())
    for compatibility_input in (
        "requirements-dev.lock",
        "pyproject.toml",
        "uv.lock",
        "requirements-landing.lock",
        "requirements-mcp.lock",
    ):
        assert (
            f'sudo cmp --silent "$release_dir/{compatibility_input}" '
            f"/opt/sidq/runtime/{compatibility_input}" in normalized_release
        )
        assert (
            f'sudo cmp --silent "$target_release/{compatibility_input}" '
            f"/opt/sidq/runtime/{compatibility_input}" in normalized_rollback
        )

    staging_at = release.index(
        'staging_dir=$(sudo mktemp -d "/opt/sidq/releases/.${release_sha}.XXXXXX")'
    )
    archive_at = release.index('git archive "$release_sha"')
    immutable_at = release.index('sudo chmod -R a-w,a+rX "$staging_dir"')
    exact_tree_at = release.index("diff --recursive --brief --no-dereference")
    final_path_move_at = release.index('sudo mv -T "$staging_dir" "$release_dir"')
    compatibility_at = release.index(
        'sudo cmp --silent "$release_dir/requirements-landing.lock"'
    )
    switch_at = release.index('sudo ln -s "releases/$release_sha"')
    assert (
        staging_at
        < archive_at
        < immutable_at
        < exact_tree_at
        < final_path_move_at
        < compatibility_at
        < switch_at
    )

    assert "previous_release=$(readlink -f /opt/sidq/current)" in release
    assert "runtime_compatible_release=$(readlink -f /opt/sidq/current)" in rollback
    for prerequisite in ("requirements-dev.lock", "pyproject.toml", "uv.lock"):
        assert f'"$release_dir/{prerequisite}"' in release
        assert f'"$previous_release/{prerequisite}"' in release
        assert f'"$target_release/{prerequisite}"' in rollback
        assert f'"$runtime_compatible_release/{prerequisite}"' in rollback


def test_the_video_runbook_fits_the_limit_and_leads_with_the_handoff() -> None:
    """The film document must describe the film that exists, honestly.

    The current film is built from real footage: a live catalog audit, the
    committed fixture replay, and one continuous session on the deployed
    console. The document has to carry the truth-label system, the declared
    playback rates, the artifact identity, and the owner-only upload gate —
    whatever the numbers are this week.
    """
    video = (ROOT / "docs/VIDEO.md").read_text()
    normalized_video = " ".join(video.lower().split())

    assert "under three minutes" in normalized_video
    # Was "burned english": the superseded cut baked the narration into the
    # frames. It no longer does — a viewer cannot turn baked words off or have
    # them machine-translated — so the record has to state where the transcript
    # went instead of asserting a property the film stopped having.
    assert "sidecar srt" in normalized_video
    assert "ILLUSTRATION" in video
    assert "LIVE CAPTURE" in video
    assert "REPRODUCIBLE OFFLINE REPLAY" in video
    # The captures keep the address bar in frame, and any speed change is
    # declared as a playback rate rather than hidden as an edit.
    for visible_capture_detail in ("address bar", "playback rate"):
        assert visible_capture_detail in normalized_video
    assert "independent receipt read" in normalized_video
    # Was the `sidq verify` terminal string. The receipt re-read is now shown
    # on the deployed page rather than in a terminal take, so the claim the
    # record must carry is the property, not that one line of output.
    assert "policy hash" in normalized_video
    assert "DECISION : BLOCK" in video or "`BLOCK`" in video
    # The document always carries the current artifact's exact identity.
    assert re.search(r"\d+\.\d{3} seconds", video)
    assert re.search(r"SHA-256[\s`:—-]*[0-9a-f]{64}", video)
    assert "not presented as a live mutation" in normalized_video
    assert "owner-only" in normalized_video
    assert "do not upload the video" in normalized_video


def test_the_browser_qa_record_covers_every_live_journey_and_viewport() -> None:
    qa = (ROOT / "docs/QA-RESULTS.md").read_text()
    normalized_qa = " ".join(qa.lower().split())

    for viewport in ("375x812", "768x1024", "1440x1000"):
        assert viewport in qa
    for journey in ("handoff", "gate-demo", "audit", "repair", "claims"):
        assert journey in qa
    assert "AccessLint" in qa and "0 violations" in qa
    assert "5/5" in qa and "HTTP 200" in qa
    assert "1 proposed, 0 proven, 1 rejected" in normalized_qa
    assert "catalog-dependent snapshot" in normalized_qa


def test_pull_request_ci_executes_the_local_action_without_publish_authority() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "uses: ./" in workflow
    assert "publish-results: false" in workflow
    assert "persist-credentials: false" in workflow
    assert "secrets." not in workflow


def test_write_capable_demo_action_stays_on_the_trusted_base_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "sidq-demo.yml").read_text()

    assert "pull_request_target:" in workflow
    assert "ref: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "persist-credentials: false" in workflow
    assert "uses: ./" in workflow


def test_dependency_lock_is_consumed_and_has_an_explicit_update_command() -> None:
    lock = ROOT / "requirements-dev.lock"
    assert lock.is_file()
    text = lock.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text
    assert "sidq==" not in text

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    makefile = (ROOT / "Makefile").read_text()
    action = (ROOT / "action.yml").read_text()
    assert "--require-hashes -r requirements-dev.lock" in workflow
    assert "--require-hashes -r requirements-dev.lock" in makefile
    assert "--require-hashes" in action and "requirements-action.lock" in action
    assert "--no-build-isolation" in action
    assert '"mcp-server-datahub==0.6.0"' not in action
    assert "live mode requires datahub-mcp-command" in action
    assert "lock:" in makefile and "uv lock" in makefile and "uv export" in makefile


def test_clean_clone_carries_the_preflight_regression_evidence() -> None:
    assert _CORPUS.is_file()
    assert _CORPUS.stat().st_size < 1_000_000
    assert (ROOT / "data" / "benchmark" / "preflight-rungs.json").is_file()
    rows = _regression_rows()
    assert len(rows) == 20_666
    assert eval_preflight.evaluate(rows)["distinct_labels"] == 3


def test_judge_copy_does_not_overstate_reproducibility_or_exclusivity() -> None:
    readme = README.read_text(encoding="utf-8")
    devpost = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")
    landing = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "the only one asking" not in readme
    assert (
        "no network"
        not in readme.split("## Judge runbook", 1)[1].split("## The questions", 1)[0]
    )
    assert "guards on every claim this README makes" not in readme
    assert "every number this README states is pinned" not in readme
    assert "carries each verdict back" not in readme
    assert "every claim" not in landing.lower()
    scope = (ROOT / SCOPE_SURFACE).read_text(encoding="utf-8")
    for surface in (readme, devpost, scope):
        normalized = " ".join(surface.lower().split())
        assert "complete-lineage regression" in normalized
        assert "live" in normalized and "fails closed" in normalized
        # The regression proves the *mechanism* — a one-hop fix is refused, the
        # whole-closure proposal is accepted. It does not prove any particular
        # column count on any particular platform: `test_repair_agent.py` runs
        # a synthetic three-node chain, and the "6 columns across dbt, Snowflake
        # and Looker" figure came from a live run whose output was never
        # committed. Describing the closure by its rule costs nothing and is
        # true of every catalog; quoting a count the repository cannot produce
        # is the defect this project exists to catch.
        assert "6 columns across" not in normalized
        assert "six columns across" not in normalized
    assert "against the live showcase catalog the engine refused it" not in readme
    assert "shipped the 6-column closure" not in landing
    assert "sidq repair --via-mcp --apply` writes it" not in readme
    assert "may write a jointly proven plan" in readme


def test_the_readme_repair_transcript_is_output_the_repository_can_produce() -> None:
    """The transcript a judge reads must be what the committed code prints.

    Banning the retired "6 columns across dbt, Snowflake and Looker" figure was
    only half the repair: the README went on quoting an engine transcript from
    that same uncommitted live run — a Looker explore and a `customer_id` column
    that appear nowhere in `test_repair_agent.py`, whose fixture is a synthetic
    three-node dbt chain on `email`. A count nobody can reproduce and a
    transcript nobody can reproduce are the same defect. This guard re-runs the
    regression and demands the README quote what actually came out.
    """
    sys.path.insert(0, str(ROOT / "tests"))
    from test_repair_agent import _PII, _pii_chain, _pii_finding, _urn

    from sidq.repair import propose, prove, render_plan

    snapshot = _pii_chain()
    closure = propose(_pii_finding(_urn("middle")), snapshot)
    assert closure is not None
    one_hop = type(closure)(
        finding_kind=closure.finding_kind,
        subject=closure.subject,
        tool=closure.tool,
        arguments={
            "tag_urns": [_PII],
            "entity_urns": [_urn("middle")],
            "column_paths": ["email"],
        },
        rationale="one hop only",
    )

    text = " ".join(README.read_text(encoding="utf-8").split())
    for plan, heading in (
        (prove(snapshot, [one_hop]), "Refused"),
        (prove(snapshot, [closure]), "Proven"),
    ):
        rendered = render_plan(plan)
        start = next(i for i, line in enumerate(rendered) if line.startswith(heading))
        # The whole block, in order — not a bag of lines. Checking membership
        # one line at a time is how a swapped subject slips through: the real
        # subject still appears further down the page, so every line is "found"
        # while the transcript as printed says something else.
        block = " ".join(
            " ".join(line.split())
            for line in rendered[start:]
            if line.strip() and not line.startswith("Nothing was written")
        )
        assert block in text, (
            "the README quotes a repair transcript the engine does not print; "
            f"expected this block verbatim: {block!r}"
        )

    # The refusal is interesting *because* the rejected repair resolved its own
    # finding. A README that hides that reads as "the fix did not work", which
    # is the opposite of the point and is contradicted by `test_repair_agent.py`.
    assert prove(snapshot, [one_hop]).rejected[0].resolved
    assert "does resolve the finding" in text.replace("*", "")


def test_submission_copy_does_not_publish_a_stale_video_or_deny_catalog_io() -> None:
    readme = README.read_text(encoding="utf-8")
    devpost = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")

    assert "The 2:26 film" not in readme
    assert "youtu.be/5izxVeQ11dY" not in readme
    assert "**Public video:** https://www.youtube.com/watch?v=W0uHsq2Kb0E" in devpost
    assert "<PUBLIC_VIDEO_URL>" not in devpost
    assert "not the input to that loop, nor its output" not in devpost


def test_one_film_identity_across_every_document_that_states_one() -> None:
    """Two linked docs describing two different films is a one-click defect.

    `QA-RESULTS.md` documented the superseded 2026-08-02 export as "the upload
    artifact" — its SHA, its byte count, its duration, its cue count — for three
    days after a different file was uploaded, while `VIDEO.md`, one README link
    away, gave the real numbers. Nothing caught it: the guards pinned the URL and
    the SHA separately, and neither noticed the documents disagreed. Every figure
    that identifies the film is pinned here, in one place, for every surface.

    The superseded values may still appear — the corrected record explains what
    changed — but only where the same passage calls them superseded, because a
    retired number a reader can still find is exactly what this project exists
    to catch.
    """
    final = {
        "b9318837f8a36fad25db310e20cfc5d5e7e71b21b66bbdcaa0c9b84ed12fa592",
        "11,802,599",
        "171.051",
    }
    superseded = {
        "0811a494c3ee6f78f907c3f2d14908ca18df403d81e38d63093cfa7dab46beef",
        "29,636,338",
        "169.216",
        "5,075",
    }
    # A passage is a blank-line paragraph, one bullet, or one table row. Whole
    # paragraphs are too coarse for CLAIMS-MATRIX.md, where the entire table is
    # one block and a policy hash three rows away would read as the film's.
    passage = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*+]\s|\|))")

    for document in sorted(ROOT.glob("docs/*.md")) + [README]:
        name = document.relative_to(ROOT)
        for chunk in passage.split(document.read_text(encoding="utf-8")):
            flat = " ".join(chunk.split())
            retired = "supersede" in flat.lower()

            stale = sorted(value for value in superseded if value in flat)
            assert not stale or retired, (
                f"{name} states {stale} without calling them superseded; "
                "a judge reading this passage learns the wrong film"
            )

            # A document need not repeat every figure, but a passage that names
            # the artifact and gives it a hash must give it the right hash —
            # that is how the wrong SHA sat in QA-RESULTS.md for three days.
            if "sidq-final-en.mp4" not in flat or retired:
                continue
            for digest in re.findall(r"\b[0-9a-f]{64}\b", flat):
                assert digest in final, (
                    f"{name} pairs the film artifact with {digest[:8]}…, which "
                    "is not the SHA-256 of the submitted file"
                )


def test_receipt_docs_define_the_fail_closed_semantic_staleness_boundary() -> None:
    readme = README.read_text(encoding="utf-8")
    devpost = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")
    receipt_spec = (ROOT / "docs" / "RECEIPT-SPEC.md").read_text(encoding="utf-8")

    assert "pinned by a test in four scripts" not in readme
    for surface in (readme, devpost, receipt_spec):
        normalized = " ".join(surface.lower().split())
        assert "semantic entity metadata" in normalized
        assert "complete one-hop upstream and downstream lineage" in normalized
        assert (
            "sidq's own receipt properties, badges, and evidence documents"
            in normalized
        )
        assert "missing, partial, or error context is stale" in normalized
        assert "policy-hash mismatch invalidates immediately" in normalized
        assert "default maximum age is 7 days" in normalized

    for judge_surface in (readme, devpost):
        normalized = " ".join(judge_surface.lower().split())
        assert "hosted public handoff alone uses 45 days" in normalized
        assert "through august 31, 2026" in normalized
        assert "context or policy change still invalidates immediately" in normalized


def test_swarm_docs_match_latest_receipt_observability() -> None:
    surfaces = (
        README.read_text(encoding="utf-8"),
        (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8"),
    )

    for surface in surfaces:
        normalized = " ".join(surface.lower().split())
        for stale_claim in (
            "receipted before",
            "receipted after",
            "duplicate examinations",
            "ledger counts collisions",
            "collisions are safe under a deterministic engine and are counted",
            "reports which worker covered what",
        ):
            assert stale_claim not in normalized
        for honest_boundary in (
            "at-least-once, never exactly-once",
            "narrows the race window but cannot remove it",
            "deterministic duplicate work is safe",
            "current recognizable non-stale receipts",
            "current-run receipts",
            "latest worker attribution",
            "unreceipted work remains eligible",
            "cannot count collisions after the fact",
        ):
            assert honest_boundary in normalized

    landing = " ".join(
        (ROOT / "web" / "index.html").read_text(encoding="utf-8").lower().split()
    )
    assert "never re-paid" not in landing
    assert (
        "a valid receipt can avoid repeat work; concurrent workers may still "
        "duplicate an examination"
    ) in (ROOT / SCOPE_SURFACE).read_text(encoding="utf-8")


def test_supported_python_copy_matches_the_single_tested_minor() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    surfaces = "\n".join(
        (ROOT / name).read_text()
        for name in ("README.md", "docs/DEVPOST.md", "docs/PR-BOT.md")
    )

    assert 'requires-python = ">=3.12,<3.13"' in project
    assert "Python 3.12 or newer" not in surfaces


def test_devpost_has_submission_fields_and_no_committed_demo_password() -> None:
    """The submission copy must name judge access without publishing a secret.

    This guard used to require the phrases "Testing instructions" and "visible to
    judges", because the plan was to hide the judge account in Devpost's private
    testing-instructions box. Checking the live submission form on 2026-08-06
    settled it: this hackathon has fourteen fields and none of them is that box.
    Anything a judge needs in order to reach the catalog is public by necessity,
    which is exactly why the published account has to be read-only. The guard now
    pins that reasoning instead of the field that does not exist, so nobody
    restores the old instruction and then wonders where to paste it.
    """
    text = (ROOT / "docs" / "DEVPOST.md").read_text()
    normalized = " ".join(text.split())

    for required in (
        "Reader",
        "<READER_USERNAME>",
        "<READER_PASSWORD>",
        "AI coding assistants",
        "pre-existing",
        "feedback",
        "Public video",
        "Repository",
        "Live project",
    ):
        assert required in text
    assert "no private field" in normalized, (
        "the copy must say why the judge account is public, or the next reader "
        "will look for a testing-instructions box that this form does not have"
    )
    assert "must be read-only" in normalized or "must therefore be read-only" in (
        normalized
    )
    assert "password `datahub`" not in text
    assert "username `datahub`" not in text


def test_committed_judge_docs_do_not_contain_default_reader_credentials() -> None:
    for path in (ROOT / "README.md", ROOT / "docs" / "DEVPOST.md"):
        lowered = path.read_text().lower()
        assert "username `datahub`" not in lowered
        assert "password `datahub`" not in lowered


def test_the_upstream_skill_contribution_is_linked_on_judge_surfaces() -> None:
    """One open PR, cited identically everywhere it is cited.

    There were briefly two upstream pull requests for this skill, and closing the
    superseded one left three documents pointing at a closed thread — a judge
    checking the bonus criterion would have followed a dead link. Asserting that
    every reference resolves to the same number is what makes the next
    supersession a build failure rather than a stale citation.
    """
    contribution = "https://github.com/datahub-project/datahub-skills/pull/81"

    cited = {
        name: (ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "docs/DEVPOST.md", "ARCHITECTURE.md")
    }
    for name, text in cited.items():
        assert contribution in text, name
        others = re.findall(
            r"datahub-project/datahub-skills/pull/(\d+)",
            text,
        )
        assert set(others) == {"81"}, (
            f"{name} cites a superseded PR: {sorted(set(others))}"
        )


def test_liveness_is_dependency_free_and_names_the_exact_demo_surface(
    monkeypatch,
) -> None:
    from web import server

    monkeypatch.delenv("SIDQ_RELEASE_SHA", raising=False)
    payload = server._health_payload()

    assert payload == {
        "status": "ok",
        "service": "sidq-landing",
        "live_demos": sorted(server.RUNNABLE),
        "release": {"state": "local/dev", "commit_sha": None},
    }


def test_release_sha_is_validated_or_derived_from_the_resolved_release_path(
    monkeypatch,
) -> None:
    from web import server

    explicit = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    monkeypatch.setenv("SIDQ_RELEASE_SHA", explicit)
    assert server._release_sha() == explicit.lower()
    assert server._health_payload()["release"] == {
        "state": "deployed",
        "commit_sha": explicit.lower(),
    }

    monkeypatch.setenv("SIDQ_RELEASE_SHA", "../../etc/passwd")
    monkeypatch.setattr(
        server,
        "REPO",
        Path("/opt/sidq/releases/0123456789abcdef0123456789abcdef01234567"),
    )
    assert server._release_sha() == "0123456789abcdef0123456789abcdef01234567"

    monkeypatch.setattr(server, "REPO", ROOT)
    assert server._release_sha() is None
    payload = server._health_payload()
    rendered = json.dumps(payload)
    assert payload["release"] == {"state": "local/dev", "commit_sha": None}
    assert "/opt/" not in rendered and str(ROOT) not in rendered


@pytest.mark.parametrize(
    ("datahub_ready", "status", "datahub"),
    ((True, "ready", "ok"), (False, "degraded", "unavailable")),
)
def test_readiness_reports_the_live_catalog_dependency(
    datahub_ready: bool, status: str, datahub: str
) -> None:
    from web import server

    payload = server._readiness_payload(lambda: datahub_ready)

    assert payload == {
        "status": status,
        "service": "sidq-landing",
        "datahub": datahub,
    }


def test_run_endpoints_reject_request_bodies_before_starting_work(monkeypatch) -> None:
    from web import server

    responses: list = []
    handler = _stub_handler(server, monkeypatch, responses, "/run/gate-demo")
    handler.headers = {"Content-Length": "1"}
    monkeypatch.setattr(
        server,
        "_run",
        lambda name: pytest.fail("a request with a body must not start a command"),
    )

    handler.do_POST()

    assert responses == [(400, {"error": "request body is not accepted"})]


def test_the_contradiction_count_and_its_concentration_are_both_true() -> None:
    """285 is the finding count; 5 is where they live. Conflating them misleads.

    The README once read "285 contradictions across 67 datasets", which a reader
    naturally hears as "spread over 67". They are not: 67 is what was examined,
    and every one of the 285 lands on five PowerBI measure assets. Hand-checking
    the published evidence is what caught it, and this pins both halves so the
    wording cannot drift back.
    """
    report = json.loads(
        (ROOT / "examples/03-catalog-truth-report/report.json").read_text()
    )
    findings = [
        item
        for item in report["evidence"]
        if item.get("kind") == "lineage_field_missing"
    ]
    affected = {item["subject"].partition("#")[0] for item in findings}

    assert len(findings) == 285
    assert len(affected) == 5

    readme = (ROOT / "README.md").read_text()
    assert "285 internal contradictions" in readme
    assert "concentrated in **5 assets**" in readme
    assert "contradictions across 67 datasets" not in readme


# ---------------------------------------------------------------------------
# Cross-references. A document we point a judge at must exist, or the pointer is
# a broken promise on the surface whose whole argument is that claims are checked.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("document", ("README.md", "docs/PREFLIGHT-RESULTS.md"))
def test_every_document_we_point_at_exists(document: str) -> None:
    path = ROOT / document
    text = path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"[(`](docs/[\w./-]+\.md|[\w-]+\.md)[)`]", text))
    missing = sorted(
        name
        for name in referenced
        if not (ROOT / name).exists() and not (path.parent / name).exists()
    )

    assert not missing, f"{document} points at: {', '.join(missing)}"


def test_the_speed_claim_and_the_measurement_agree() -> None:
    """The README's speed sentence is the summary of a document; both must say it.

    `PREFLIGHT-RESULTS.md` reports a tie on accuracy, and a tie is the kind of
    result that quietly invites a model back in. The speed measurement is what
    makes the tie a decision — so the claim has to travel with the evidence
    rather than living alone in a summary nobody regenerates.
    """
    cost = ROOT / "docs" / "DECISION-COST.md"
    assert cost.exists(), "the speed measurement must be published, not just run"

    text = cost.read_text(encoding="utf-8")
    assert "three orders of magnitude" in text
    # The friendliest framing for the model is published too, or the comparison
    # is a selected one.
    assert "amortised over" in text

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "three orders of magnitude slower" in readme
    assert "docs/DECISION-COST.md" in readme


def test_the_gpu_objection_is_answered_with_a_measurement() -> None:
    """ "You benchmarked a CPU" is the first thing anyone will say about that claim.

    It is answered by running the same fitted weights on real CUDA hardware, and
    the evidence is committed rather than described — including the one framing
    where the GPU genuinely wins, which is the row a selective report would drop.
    """
    evidence = json.loads(
        (ROOT / "data" / "benchmark" / "decision-cost-gpu.json").read_text()
    )
    for key in ("gpu", "torch", "rows", "rule_ns", "roundtrip_ns", "batch_ns"):
        assert key in evidence, f"the GPU evidence lost {key}"

    document = (ROOT / "docs" / "DECISION-COST.md").read_text(encoding="utf-8")
    assert evidence["gpu"] in document
    assert "faster**" in document, (
        "the batch row is the framing that favours the GPU; publishing the "
        "comparison without it would be the selective reporting this refuses"
    )
    # The honest framing must stay the headline: a decision is not delivered
    # until it has crossed back to the host.
    assert evidence["roundtrip_ns"] > evidence["resident_ns"] > evidence["rule_ns"]


# ---------------------------------------------------------------------------
# The documentation reader. Its numbers are a published claim like any other,
# and the committed evaluation report is what they have to keep matching.
# ---------------------------------------------------------------------------


def test_the_reader_document_quotes_the_report_it_was_trained_from() -> None:
    """Retraining moves these numbers; the document must move with them."""
    report = json.loads(
        (ROOT / "data" / "claims" / "reader" / "report.json").read_text()
    )
    text = (ROOT / "docs" / "CLAIM-READER.md").read_text(encoding="utf-8")

    point = report["operating_point"]
    assert f"{point['precision']:.1%}" in text
    assert f"{point['recall']:.1%}" in text
    assert f"{report['train_rows']:,} rows" in text
    assert report["embedding_model"] in text
    assert report["embedding_revision"] in text
    assert report["chosen"] == "logistic regression", (
        "the shipped head changed; docs/CLAIM-READER.md explains why the linear "
        "one was chosen and must be rewritten before a different one ships"
    )


def test_the_reader_only_ever_proposes_fully_specified_claim_types() -> None:
    """A classifier produces a label, not arguments.

    `accepted_values` needs its value list and `relationships` needs its target.
    Proposing one without them would compile a query testing something nobody
    documented, so the trainer and the shipped reader must agree on the same
    two-item allow-list — separately defined, hence separately checked.
    """
    from sidq.claims import reader as shipped

    report = json.loads(
        (ROOT / "data" / "claims" / "reader" / "report.json").read_text()
    )

    assert tuple(report["proposable"]) == shipped._PROPOSABLE
    assert set(shipped._PROPOSABLE) == {"unique", "not_null"}


def test_the_rules_versus_model_comparison_quotes_the_same_report() -> None:
    """Both sides of that table are scored in one run; the document must match it.

    The comparison is the project's own argument turned on itself — the same
    corpus, the same code path, and a rule that wins one task and loses the
    other. It is only worth publishing while the numbers are the measured ones.
    """
    report = json.loads(
        (ROOT / "data" / "claims" / "reader" / "report.json").read_text()
    )
    text = (ROOT / "docs" / "CLAIM-READER.md").read_text(encoding="utf-8")

    baseline = report["rule_baseline"]
    assert f"{baseline['precision']:.1%}" in text
    assert f"{baseline['recall']:.1%}" in text
    assert f"| {int(baseline['proposals'])} |" in text
    # The rule losing this task is the whole point of the section; if it ever
    # wins, the document argues for something that is no longer true.
    assert baseline["precision"] < report["operating_point"]["precision"]


# ---------------------------------------------------------------------------
# The model and privacy boundary. `sidq claims` widened the product after the
# operations copy was written; these guards keep the old, narrower claims from
# silently surviving on the two surfaces a judge reads first.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("document", ("README.md", SCOPE_SURFACE))
def test_privacy_copy_scopes_catalog_reads_and_live_source_checks(
    document: str,
) -> None:
    """Catalog audits are metadata-only; source attestation is a distinct opt-in.

    Saying the whole product "never reads row data" became false when
    `sidq claims` began running bounded read-only SQL against a live source. The
    stronger, useful property is that query results stay local and never become
    model input, while the ordinary catalog audit remains metadata-only.
    """
    text = (ROOT / document).read_text(encoding="utf-8")
    lowered = text.lower()

    assert "catalog audits read metadata only" in lowered
    assert "sidq claims" in text
    assert "read-only sql" in lowered
    assert "query results never enter a model" in lowered
    assert "sidq reads metadata only" not in lowered
    assert "metadata only, nothing leaves" not in lowered


@pytest.mark.parametrize("document", ("README.md", SCOPE_SURFACE))
def test_model_drift_copy_names_the_optional_reader_boundary(document: str) -> None:
    """The optional reader may change warnings, but it can never grant or block.

    The previous copy collapsed two claims: the blocking path has no model, but
    the optional documentation reader does. Its revision and head are pinned so
    warning coverage is attributable instead of drifting invisibly.
    """
    text = (ROOT / document).read_text(encoding="utf-8").lower()

    assert "no model can block" in text
    assert "optional documentation reader" in text
    assert "pinned revision" in text
    assert "head fingerprint" in text
    assert "no model, no drift" not in text


def test_every_architecture_surface_contains_the_same_svg() -> None:
    """One edited diagram must not leave the landing page or gallery stale."""
    canonical = (ROOT / "docs" / "architecture.svg").read_text(encoding="utf-8")
    web = (ROOT / "web" / "architecture.svg").read_text(encoding="utf-8")
    gallery = (ROOT / "docs" / "gallery" / "src" / "03-architecture.html").read_text(
        encoding="utf-8"
    )
    embedded = gallery[gallery.index("<svg") : gallery.index("</svg>") + len("</svg>")]

    assert web == canonical
    assert embedded == canonical.strip()


def test_architecture_draws_the_model_outside_the_judged_path() -> None:
    """The picture must answer the model question without a paragraph."""
    diagram = (ROOT / "docs" / "architecture.svg").read_text(encoding="utf-8")

    for label in (
        "OPTIONAL PROSE READER",
        "READ-ONLY SOURCE CHECK",
        "DATAHUB — SHARED CURRENT STATE",
        "NO MODEL CAN BLOCK",
    ):
        assert label in diagram
    assert (
        "READS AND WRITES ONLY THROUGH THE OFFICIAL DATAHUB MCP SERVER" not in diagram
    )


@pytest.mark.parametrize(
    "document",
    (
        "README.md",
        "ARCHITECTURE.md",
        SCOPE_SURFACE,
        "docs/architecture.svg",
    ),
)
def test_judge_surfaces_reject_ledger_and_proof_overclaims(document: str) -> None:
    text = (ROOT / document).read_text(encoding="utf-8")
    lowered = text.lower()

    for overclaim in (
        "catalog is the ledger",
        "context + ledger",
        "every agent proved",
        "nobody gates",
    ):
        assert overclaim not in lowered
    assert "shared current state" in lowered


@pytest.mark.parametrize("document", ("README.md", "ARCHITECTURE.md", SCOPE_SURFACE))
def test_shared_state_copy_names_latest_values_and_optional_receipt_writes(
    document: str,
) -> None:
    lowered = (ROOT / document).read_text(encoding="utf-8").lower()

    assert "not append-only" in lowered
    assert "optional" in lowered and "receipt" in lowered


def test_architecture_png_is_the_full_size_regenerated_board() -> None:
    png = (ROOT / "docs" / "gallery" / "03-architecture.png").read_bytes()

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (1920, 1080)


def test_architecture_gap_claim_names_sidqs_contribution_without_absolutes() -> None:
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    gap = architecture.split("## The gap", 1)[1].split("## The flow", 1)[0]

    assert "Nobody gates" not in gap
    assert "Sidq's contribution" in gap
    assert "deterministic, DataHub-native pre-merge refusal path" in gap


def test_devpost_resume_claim_stays_within_latest_value_and_race_boundaries() -> None:
    devpost = (ROOT / "docs" / "DEVPOST.md").read_text(encoding="utf-8")
    lowered = " ".join(devpost.lower().split())

    assert "any Sidq instance resumes where any other stopped" not in devpost
    assert "latest receipt values" in lowered
    assert "not append-only history" in lowered
    assert "does not provide exactly-once coordination" in lowered


def test_architecture_flow_marks_receipt_write_as_operator_enabled_and_optional() -> (
    None
):
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    flow = architecture.split("## The flow", 1)[1].split(
        "## Three delivery surfaces", 1
    )[0]

    assert "[S]  SIDQ RECEIPT" in flow
    assert "optional, operator-enabled write of current, queryable values" in flow
    assert "explicit, queryable, written back onto the affected assets" not in flow


def test_readme_swarm_demo_names_the_current_state_observer_not_a_ledger() -> None:
    readme = README.read_text(encoding="utf-8")

    assert (
        "make swarm-demo    # four workers, one killed mid-run, then the current-state observer"
        in readme
    )
    assert (
        "make swarm-demo    # four workers, one killed mid-run, then the ledger"
        not in readme
    )


def test_architecture_names_the_current_delivery_surfaces_and_mcp_tools() -> None:
    """The architecture must not advertise the retired two-tool MCP contract."""

    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "Three delivery surfaces, one engine" in architecture
    for tool in ("check_change", "verify_context", "search_verified"):
        assert f"`{tool}" in architecture
    assert "get_verification_status" not in architecture


def test_landing_calls_its_buttons_live_demos_not_all_agents() -> None:
    """Five agent capabilities are backed by exactly five safe live demos.

    This used to pin one exact sentence, which made it a copy-editing tripwire
    rather than a claim guard: rewording the line failed the build while
    changing the number of buttons did not. What matters is that the page says
    five and that five is what the server will actually run, so both halves are
    checked against each other instead.
    """
    landing = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    server = (ROOT / "web" / "server.py").read_text(encoding="utf-8")

    assert re.search(r"\bFive live proofs\b", landing), (
        "the landing page must state how many live proofs it offers"
    )

    runnable = server[server.index("RUNNABLE:") :]
    runnable = runnable[: runnable.index("\nCLIENT_RUN_LIMIT")]
    offered = set(re.findall(r'^    "([a-z-]+)":', runnable, re.MULTILINE))
    wired = set(re.findall(r'data-run="([a-z-]+)"', landing))

    assert len(offered) == 5, f"the server offers {len(offered)} commands, not five"
    assert wired == offered, (
        f"the page's buttons and the server's closed table disagree: "
        f"page-only {sorted(wired - offered)}, server-only {sorted(offered - wired)}"
    )


def test_every_button_time_on_the_page_is_inside_the_server_s_own_ceiling() -> None:
    """The page promises a duration; the server decides when to give up.

    Nothing tied those two numbers together, and they drifted: the live-source
    button advertised five seconds while a measured run on the host took
    thirty-five. A judge who clicks that and waits seven times the promise does
    not conclude the demo is thorough — the page says these times are measured
    on this host, and a number that large a judge can check by waiting is the
    worst place to be loose.

    A duration cannot be measured in CI, which has no DataHub. What CI can hold
    is the relationship: a promise longer than the server's own timeout is a
    promise the server will always break.
    """
    from web.server import EXPECTED_SECONDS, RUNNABLE

    page = (ROOT / "web/index.html").read_text(encoding="utf-8")
    advertised = re.findall(r'data-run="([a-z-]+)"[^>]*>[^<]*·\s*~(\d+)s', page)
    assert advertised, "no button on the page advertises a duration any more"

    for name, seconds in advertised:
        assert name in RUNNABLE, f"the page advertises {name}, which is not runnable"
        ceiling = EXPECTED_SECONDS[name]
        assert int(seconds) <= ceiling, (
            f"the page promises {name} in ~{seconds}s but the server gives up at "
            f"{ceiling}s, so that promise can never be kept"
        )
