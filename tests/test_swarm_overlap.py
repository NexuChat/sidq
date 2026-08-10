"""The swarm's claimed work split, measured at its honest evidence boundary.

DataHub keeps only the latest receipt, so these tests do not pretend the
catalog can reconstruct collisions.  They exercise the surviving workers'
own reports and keep that self-reported measurement separate from the
independent coverage ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidq import cli
from sidq.agent.swarm import WorkerRun
from sidq.agent.swarm_overlap import (
    SwarmReportError,
    load_reports,
    measure_overlap,
    parse_report,
    write_worker_report,
)


def _report(
    worker_id: str,
    examined: list[str],
    *,
    swarm_run: str = "swarm-1",
    written: list[str] | None = None,
    verdicts: dict[str, str] | None = None,
) -> dict[str, object]:
    written = list(examined if written is None else written)
    # Two workers that examined the same asset agree by default, because that is
    # the case the engine's determinism predicts; a test that wants divergence
    # says so by passing a different digest for one of them.
    verdicts = (
        {urn: f"digest-of-{urn}" for urn in examined} if verdicts is None else verdicts
    )
    return {
        "worker_id": worker_id,
        "swarm_run": swarm_run,
        "examined": examined,
        "written": written,
        "verdicts": verdicts,
        "summary": {
            "worker_id": worker_id,
            "swarm_run": swarm_run,
            "examined": len(examined),
            "receipts_written": len(written),
            "findings": 0,
            "vouched_by_peer": 0,
            "vouched_unattributed": 0,
            "blocked_by_receipt": 0,
            "write_failures": 0,
        },
    }


def test_disjoint_workers_produce_zero_duplicated_examinations() -> None:
    reports = [
        parse_report(_report("alpha", ["urn:a", "urn:b"])),
        parse_report(_report("beta", ["urn:c", "urn:d"])),
    ]

    overlap = measure_overlap(reports, expected_reports=2)

    assert overlap.total_examinations == 4
    assert overlap.distinct_assets_examined == 4
    assert overlap.duplicated_examinations == 0
    assert overlap.overlapping_assets == {}


def test_fully_overlapping_workers_name_every_shared_asset_and_worker() -> None:
    reports = [
        parse_report(_report("alpha", ["urn:a", "urn:b"])),
        parse_report(_report("beta", ["urn:a", "urn:b"])),
        parse_report(_report("gamma", ["urn:a", "urn:b"])),
    ]

    overlap = measure_overlap(reports, expected_reports=3)

    assert overlap.total_examinations == 6
    assert overlap.distinct_assets_examined == 2
    assert overlap.duplicated_examinations == 4
    assert overlap.overlapping_assets == {
        "urn:a": ("alpha", "beta", "gamma"),
        "urn:b": ("alpha", "beta", "gamma"),
    }


def test_a_missing_killed_worker_report_is_visible_not_an_error(
    tmp_path: Path,
) -> None:
    survived = tmp_path / "alpha.json"
    survived.write_text(json.dumps(_report("alpha", ["urn:a"])), encoding="utf-8")
    killed = tmp_path / "delta.json"

    reports = load_reports([survived, killed])
    overlap = measure_overlap(reports, expected_reports=2)

    assert overlap.found_reports == 1
    assert overlap.expected_reports == 2
    assert "worker reports found     1 of 2 expected" in "\n".join(overlap.render())


def test_a_truncated_json_report_is_a_clear_error(tmp_path: Path) -> None:
    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"worker_id":"alpha",', encoding="utf-8")

    with pytest.raises(SwarmReportError, match=r"truncated\.json: malformed JSON"):
        load_reports([truncated])


def test_render_names_the_worker_account_evidence_boundary() -> None:
    overlap = measure_overlap(
        [parse_report(_report("alpha", ["urn:a"]))], expected_reports=1
    )

    rendered = "\n".join(overlap.render())

    assert "measured from what each surviving worker reported about itself" in rendered
    assert "not from DataHub" in rendered
    assert "reporting against itself rather than flattering itself" in rendered
    assert "coverage ledger that follows is still read independently" in rendered


def test_worker_report_contains_urn_lists_and_existing_summary_counts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alpha.json"
    run = WorkerRun(
        worker_id="alpha",
        swarm_run="swarm-1",
        examined=["urn:b", "urn:a"],
        written=["urn:a"],
        verdicts={"urn:b": "digest-b", "urn:a": "digest-a"},
    )

    write_worker_report(run, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["worker_id"] == "alpha"
    assert payload["swarm_run"] == "swarm-1"
    assert payload["examined"] == ["urn:a", "urn:b"]
    assert payload["written"] == ["urn:a"]
    assert payload["summary"] == run.summary()
    assert not list(tmp_path.glob(".alpha.json.*.tmp"))


def test_swarm_cli_writes_the_requested_report_after_the_run(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "alpha.json"
    run = WorkerRun(
        worker_id="alpha",
        swarm_run="swarm-1",
        examined=["urn:a"],
        written=["urn:a"],
        verdicts={"urn:a": "digest-a"},
    )
    caller = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: caller)
    monkeypatch.setattr(
        cli,
        "SwarmWorker",
        lambda *args, **kwargs: SimpleNamespace(run=lambda: run),
    )
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "a" * 40)

    code = cli.main(
        [
            "swarm",
            "--worker-id",
            "alpha",
            "--swarm-run",
            "swarm-1",
            "--report",
            str(path),
        ]
    )

    assert code == 0
    assert json.loads(path.read_text(encoding="utf-8"))["examined"] == ["urn:a"]


def test_swarm_overlap_cli_reads_a_directory_and_rejects_bad_reports(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "alpha.json").write_text(
        json.dumps(_report("alpha", ["urn:a"])), encoding="utf-8"
    )
    (tmp_path / "beta.json").write_text(
        json.dumps(_report("beta", ["urn:a"])), encoding="utf-8"
    )

    assert cli.main(["swarm-overlap", str(tmp_path)]) == 0
    assert "duplicated examinations  1" in capsys.readouterr().out

    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert cli.main(["swarm-overlap", str(tmp_path)]) == 2
    assert "broken.json: malformed JSON" in capsys.readouterr().err


def test_a_run_with_no_surviving_reports_is_refused_not_scored() -> None:
    """Zero duplication from zero workers is the best score and the worst answer.

    When every worker crashed before writing its report, the counters were all
    honestly zero and the demo printed "duplicated examinations 0" — a perfect
    result from a run that examined nothing. The expected-versus-found line was
    right there and still lost the argument to the headline number, which is
    exactly the failure this project refuses everywhere else: an unperformed
    measurement must never render as a clean one.
    """
    with pytest.raises(SwarmReportError) as raised:
        measure_overlap([], expected_reports=4)

    message = str(raised.value)
    assert "no worker reports were found of 4 expected" in message
    assert "no work was done" in message


def test_zero_expected_and_zero_reports_is_still_allowed() -> None:
    """Asking about nothing is not the same as failing to measure something."""
    overlap = measure_overlap([], expected_reports=0)

    assert overlap.total_examinations == 0
    assert overlap.duplicated_examinations == 0


def test_overlap_cross_checks_the_duplicated_work_instead_of_only_counting_it() -> None:
    """The duplication is spent on evidence rather than merely regretted.

    Two workers reaching one asset is the only moment in the whole system when
    the same examination is performed twice by independent processes. Counting
    that as waste throws away the one measurement that duplication is uniquely
    positioned to make.
    """
    overlap = measure_overlap(
        [
            parse_report(_report("alpha", ["urn:a", "urn:b"])),
            parse_report(_report("beta", ["urn:b", "urn:c"])),
        ]
    )

    assert overlap.duplicated_examinations == 1
    assert overlap.agreed_assets == ("urn:b",)
    assert overlap.diverged_assets == {}
    # The assets only one worker reached are not cross-checked by anything.
    assert overlap.summary()["cross_checked_assets"] == 1


def test_two_workers_concluding_different_things_is_reported_not_averaged() -> None:
    """A divergence is the finding, and it survives to the rendered output.

    The engine is claimed to be deterministic, so two workers examining one
    asset must reach one conclusion. If they do not, the honest report is the
    disagreement itself — with both digests, so a reader can go and see which
    one their own re-run reproduces.
    """
    overlap = measure_overlap(
        [
            parse_report(_report("alpha", ["urn:a"], verdicts={"urn:a": "aaaa1111"})),
            parse_report(_report("beta", ["urn:a"], verdicts={"urn:a": "bbbb2222"})),
        ]
    )

    assert overlap.agreed_assets == ()
    assert overlap.diverged_assets == {
        "urn:a": {"alpha": "aaaa1111", "beta": "bbbb2222"}
    }

    rendered = "\n".join(overlap.render())
    assert "DIVERGENCE" in rendered
    assert "aaaa1111" in rendered and "bbbb2222" in rendered
    # It must not pick a winner between the two available readings.
    assert "did not read the same catalog" in rendered
    assert "engine is not deterministic" in rendered


def test_a_report_that_examined_an_asset_without_saying_what_it_concluded_is_refused() -> (
    None
):
    """A gap in the digests must not read downstream as "not compared".

    Silently tolerating a missing verdict would let a worker examine an asset,
    decline to say what it found, and have the overlap report treat that asset
    as though no peer had ever reached it.
    """
    report = _report("alpha", ["urn:a", "urn:b"])
    del report["verdicts"]["urn:b"]  # type: ignore[index]

    with pytest.raises(SwarmReportError) as raised:
        parse_report(report)

    assert "verdicts do not cover examined" in str(raised.value)
    assert "1 examined without a verdict" in str(raised.value)


def test_a_report_a_worker_can_write_is_a_report_the_reader_accepts(
    tmp_path: Path,
) -> None:
    """The writer and the reader must not disagree about what a valid report is.

    Round-tripping is the only way to know: the parser's rules were tightened to
    demand a verdict per examination, and nothing would have caught a writer
    that kept emitting reports without them until a real swarm run failed to
    measure itself.
    """
    path = tmp_path / "alpha.json"
    run = WorkerRun(
        worker_id="alpha",
        swarm_run="swarm-1",
        examined=["urn:a", "urn:b"],
        written=["urn:a"],
        verdicts={"urn:a": "digest-a", "urn:b": "digest-b"},
    )

    write_worker_report(run, path)
    [account] = load_reports([path])

    assert account.verdicts == {"urn:a": "digest-a", "urn:b": "digest-b"}


def test_a_worker_that_cannot_say_what_it_concluded_does_not_publish(
    tmp_path: Path,
) -> None:
    """Refused where it is written, not where someone else tries to read it."""
    run = WorkerRun(
        worker_id="alpha",
        swarm_run="swarm-1",
        examined=["urn:a"],
        written=[],
    )

    with pytest.raises(SwarmReportError, match="verdicts do not cover"):
        write_worker_report(run, tmp_path / "alpha.json")

    assert not list(tmp_path.iterdir()), "a refused report left a file behind"
