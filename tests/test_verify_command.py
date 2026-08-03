"""Reading a receipt back: the quarter of the loop a writer cannot prove itself.

A writer reporting its own success proves nothing. These pin the judgment a
*separate* reader makes about a receipt it found — and specifically that the
three questions it answers stay apart. Whether the receipt *applies* is not
whether it *authorizes*, and neither is whether the asset was *examined*. A
current refusal is the case where all three differ at once: it applies, it
authorizes nothing, and it means the asset was very much looked at.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from sidq import cli
from sidq.cli import main
from sidq.receipt import Action, ReceiptState, judge, render_verification

_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.consumed,DEV)"


def _status(**overrides: Any) -> dict[str, Any]:
    status = {
        "urn": _URN,
        "verdict": "PASS",
        "reason_code": None,
        "commit_sha": "a" * 40,
        "checked_at": "2026-07-30T00:00:00+00:00",
        "policy_hash": "sha256:deadbeef",
        "rules_fired": [],
        "verifier": "sidq",
        "evidence_url": "",
        "stale": False,
        "stale_reason": "",
    }
    status.update(overrides)
    return status


def test_a_current_pass_covers_the_asset_and_authorizes_continuing() -> None:
    judgment = judge(_status())

    assert judgment.state is ReceiptState.CURRENT
    assert judgment.action is Action.CONTINUE
    assert judgment.covers
    assert judgment.may_continue


def test_a_current_warn_covers_the_asset_but_only_authorizes_review() -> None:
    """WARN is not a refusal and not a green light; it is a handoff to a person."""
    judgment = judge(_status(verdict="WARN"))

    assert judgment.state is ReceiptState.CURRENT
    assert judgment.action is Action.REVIEW
    assert judgment.covers
    assert not judgment.may_continue


def test_a_current_block_covers_the_asset_and_authorizes_nothing() -> None:
    """The case the old boolean got wrong in both directions at once.

    It was reported as uncovered — which starved the resume path into
    re-examining refused assets forever — and rendered as `NOT VERIFIED`, which
    told a reader nobody had looked. A refusal is the most examined an asset
    ever gets. What it must never do is authorize acting.
    """
    judgment = judge(_status(verdict="BLOCK", reason_code="PII_EXPOSURE"))

    assert judgment.state is ReceiptState.CURRENT
    assert judgment.action is Action.STOP
    assert judgment.covers
    assert judgment.refused
    assert not judgment.may_continue
    assert "PII_EXPOSURE" in judgment.reason


def test_an_absent_receipt_covers_nothing_and_authorizes_nothing() -> None:
    """The failure this whole project exists to prevent: unchecked reading as clean."""
    judgment = judge(_status(verdict=None))

    assert judgment.state is ReceiptState.ABSENT
    assert judgment.action is Action.RECHECK
    assert not judgment.covers
    assert "no receipt" in judgment.reason


def test_a_stale_receipt_covers_nothing_and_says_why() -> None:
    judgment = judge(_status(stale=True, stale_reason="policy_hash changed"))

    assert judgment.state is ReceiptState.STALE
    assert judgment.action is Action.RECHECK
    assert not judgment.covers
    assert "policy_hash changed" in judgment.reason


def test_a_stale_block_is_stale_rather_than_a_refusal_that_still_stands() -> None:
    """Applicability is settled before the verdict is even read.

    A refusal about an asset that has since changed is not a standing refusal —
    it is a decision whose subject moved. Reporting it as a current BLOCK would
    both overstate what is known and, worse, let it count as coverage.
    """
    judgment = judge(
        _status(
            verdict="BLOCK",
            reason_code="PII_EXPOSURE",
            stale=True,
            stale_reason="asset decision context changed",
        )
    )

    assert judgment.state is ReceiptState.STALE
    assert judgment.action is Action.RECHECK
    assert not judgment.covers
    assert not judgment.refused
    assert "asset decision context changed" in judgment.reason


@pytest.mark.parametrize("invented", ["APPROVED", "pass", "OK", "BLOCKED", " PASS"])
def test_an_invented_verdict_grants_neither_coverage_nor_action(invented: str) -> None:
    """A catalog cannot mint a fourth verdict and have it read as anything."""
    judgment = judge(_status(verdict=invented))

    assert judgment.state is ReceiptState.INVALID
    assert judgment.action is Action.RECHECK
    assert not judgment.covers
    assert not judgment.may_continue


def test_a_status_that_is_not_a_mapping_is_unreadable_rather_than_trusted() -> None:
    judgment = judge(None)  # type: ignore[arg-type]

    assert judgment.state is ReceiptState.INVALID
    assert not judgment.covers
    assert not judgment.may_continue


def test_only_absent_stale_and_unreadable_receipts_render_as_not_verified() -> None:
    """`NOT VERIFIED` is reserved for "nobody could tell you", nothing else."""
    current = {
        verdict: render_verification(_URN, _status(verdict=verdict))[0]
        for verdict in ("PASS", "WARN", "BLOCK")
    }

    assert current["PASS"].startswith("CURRENT RECEIPT · PASS · CONTINUE")
    assert current["WARN"].startswith("CURRENT RECEIPT · WARN · REVIEW_OR_ESCALATE")
    assert current["BLOCK"].startswith("CURRENT RECEIPT · BLOCK · STOP")
    assert not any("NOT VERIFIED" in line for line in current.values())

    for absent_or_stale in (_status(verdict=None), _status(stale=True)):
        assert render_verification(_URN, absent_or_stale)[0].startswith("NOT VERIFIED")


def test_the_rendering_shows_the_provenance_the_verdict_rests_on() -> None:
    lines = render_verification(_URN, _status(rules_fired=["pii_exposure"]))

    text = "\n".join(lines)
    assert text.startswith("CURRENT RECEIPT · PASS · CONTINUE  ")
    assert "sha256:deadbeef" in text
    assert "pii_exposure" in text


def test_a_failed_readback_is_not_confused_with_a_failed_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 means the catalog could not be read; 1 means it was, and said no."""

    class _Broken:
        def __call__(self, name: str, arguments: Any) -> Any:
            raise RuntimeError("no MCP server")

        def close(self) -> None:
            return None

    monkeypatch.setattr("sidq.cli.StdioMCPToolCaller", _Broken)
    monkeypatch.setattr(
        "sidq.cli.StdioMCPReceiptToolCaller",
        lambda *args, **kwargs: pytest.fail(
            "verify must not start a mutation-enabled MCP transport"
        ),
    )

    assert main(["verify", _URN]) == 2
    assert "could not read the receipt" in capsys.readouterr().err


def test_verify_exits_one_when_the_asset_has_no_receipt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Empty:
        def __call__(self, name: str, arguments: Any) -> Any:
            return {"entities": [{"urn": _URN}]}

        def close(self) -> None:
            return None

    monkeypatch.setattr("sidq.cli.StdioMCPToolCaller", _Empty)
    monkeypatch.setattr(
        "sidq.cli.StdioMCPReceiptToolCaller",
        lambda *args, **kwargs: pytest.fail(
            "verify must not start a mutation-enabled MCP transport"
        ),
    )

    assert main(["verify", _URN]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_verify_max_age_days_is_typed_and_defaults_to_seven() -> None:
    default = cli._parser().parse_args(["verify", _URN])
    configured = cli._parser().parse_args(["verify", _URN, "--max-age-days", "45"])

    assert default.max_age_days == 7
    assert configured.max_age_days == 45
    assert isinstance(configured.max_age_days, int)


def test_verify_passes_the_configured_maximum_age_to_the_receipt_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Reader:
        def close(self) -> None:
            return None

    def read_status(urn: str, caller: object, **kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return _status()

    monkeypatch.setattr(cli, "StdioMCPToolCaller", _Reader)
    monkeypatch.setattr(cli, "get_verification_status", read_status)

    assert main(["verify", _URN, "--max-age-days", "45"]) == 0
    assert seen["max_age"] == timedelta(days=45)


def test_verify_rejects_a_negative_maximum_age_before_opening_a_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "StdioMCPToolCaller",
        lambda: pytest.fail("invalid arguments must not open an MCP transport"),
    )

    with pytest.raises(SystemExit) as failure:
        main(["verify", _URN, "--max-age-days", "-1"])

    assert failure.value.code == 2
