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

import json
import re
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
_CORPUS = ROOT / "data" / "benchmark" / "labelled.jsonl"
needs_corpus = pytest.mark.skipif(
    not _CORPUS.exists(),
    reason=f"{_CORPUS.relative_to(ROOT)} is not committed; regenerate it with "
    "scripts/generate_mutations.py + scripts/label_mutations.py to run this guard",
)
README = ROOT / "README.md"
TRUTH_REPORT = ROOT / "examples" / "03-catalog-truth-report" / "report.json"


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


def test_the_unowned_asset_count_matches_the_evidence() -> None:
    entry = _summary()["unowned_consumed"]

    assert f"{entry['findings']} consumed-but-unowned assets" in README.read_text(
        encoding="utf-8"
    )


def test_the_powerbi_example_numbers_match_the_evidence() -> None:
    """The README singles out one asset: 58 lineage edges, 57 targeting nothing."""
    raw = json.loads(TRUTH_REPORT.read_text(encoding="utf-8"))
    text = README.read_text(encoding="utf-8")
    subject = "Customer_Analytics_Measures"

    findings = [
        item
        for item in raw["evidence"]
        if item["kind"] == "lineage_field_missing" and subject in item["subject"]
    ]
    assert findings, f"{subject} must still appear in the audit evidence"

    claimed = {
        int(value) for value in re.findall(r"\*\*(\d+) column-lineage edges", text)
    }
    claimed_missing = {
        int(value) for value in re.findall(r"\*\*(\d+) of which target fields", text)
    }
    assert claimed, "the README must still quote the edge count"
    assert claimed_missing, "the README must still quote the missing-target count"
    assert max(claimed_missing) == len(findings), (
        f"README claims {max(claimed_missing)} missing targets for {subject}; "
        f"the evidence file has {len(findings)}"
    )


def test_the_unverifiable_negative_result_is_still_honest() -> None:
    """The README's strongest honesty signal: 32/32 lineage_rot unverifiable."""
    text = README.read_text(encoding="utf-8")

    assert "32/32" in text
    assert "unverifiable" in text


# ---------------------------------------------------------------------------
# The pre-flight negative result. PREFLIGHT.md §6 pre-commits to publishing it.
# ---------------------------------------------------------------------------


@needs_corpus
def test_the_preflight_result_matches_the_corpus_it_reports_on() -> None:
    """A published negative result is still a claim, and must stay true."""
    assert eval_preflight.render(
        eval_preflight.evaluate(eval_preflight.load())
    ) == eval_preflight.DOCUMENT.read_text(encoding="utf-8"), (
        "docs/PREFLIGHT-RESULTS.md is stale; rerun scripts/eval_preflight.py"
    )


@needs_corpus
def test_the_corpus_input_contract_has_no_verdict_leak() -> None:
    """§3's leak rule, enforced mechanically rather than by reviewer vigilance.

    A feature built from the verdict would score beautifully and be worthless —
    the failure mode §3 calls hardest to notice after the fact.
    """
    result = eval_preflight.evaluate(eval_preflight.load())

    assert result["leaked_keys"] == []
    assert result["unexpected_keys"] == []


def test_preflight_is_not_advertised_as_shipped_while_it_is_not() -> None:
    """The spec is a binding contract; it must not read as delivered capability."""
    spec = (ROOT / "docs" / "PREFLIGHT.md").read_text(encoding="utf-8")
    results = eval_preflight.DOCUMENT.read_text(encoding="utf-8")

    assert "not shipped" in spec.lower()
    assert "not shipped" in results.lower()


@needs_corpus
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


@needs_corpus
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

JUDGE_FACING = ("README.md", "web/index.html", "docs/SETUP.md", "docs/DEVPOST.md")


def _make_targets() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-zA-Z][\w-]*):", text, flags=re.MULTILINE))


@pytest.mark.parametrize("document", JUDGE_FACING)
def test_every_make_command_we_publish_exists(document: str) -> None:
    """A published instruction that fails is worse than no instruction."""
    text = (ROOT / document).read_text(encoding="utf-8")
    referenced = set(re.findall(r"\bmake ([a-z][\w-]*)", text))
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
        # The only two flags that make a sidq command mutate a catalog. A bare
        # `repair` is a dry run by construction — the CLI writes only under
        # --apply, and tests/test_repair_agent.py pins that a dry run performs
        # zero tool calls — so the guard names the write paths, not the word.
        assert "--apply" not in argv and "--write-receipts" not in argv, (
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
