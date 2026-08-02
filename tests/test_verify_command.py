"""Reading a receipt back: the quarter of the loop a writer cannot prove itself.

A writer reporting its own success proves nothing. These pin the judgment a
*separate* reader makes about a receipt it found — and specifically that the three
ways a receipt can fail to vouch for an asset (absent, stale, refusal) never
collapse into the same answer as "checked and passed".
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from sidq import cli
from sidq.cli import main
from sidq.receipt import holds, render_verification

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


def test_a_pass_receipt_that_still_applies_verifies() -> None:
    verified, reason = holds(_status())

    assert verified
    assert "PASS" in reason


def test_an_absent_receipt_is_not_verified() -> None:
    """The failure this whole project exists to prevent: unchecked reading as clean."""
    verified, reason = holds(_status(verdict=None))

    assert not verified
    assert "no receipt" in reason


def test_a_stale_receipt_is_not_verified_and_says_why() -> None:
    verified, reason = holds(_status(stale=True, stale_reason="policy_hash changed"))

    assert not verified
    assert "policy_hash changed" in reason


def test_a_block_receipt_is_not_verified() -> None:
    verified, reason = holds(_status(verdict="BLOCK", reason_code="PII_EXPOSURE"))

    assert not verified
    assert "PII_EXPOSURE" in reason


def test_the_rendering_shows_the_provenance_the_verdict_rests_on() -> None:
    lines = render_verification(_URN, _status(rules_fired=["pii_exposure"]))

    text = "\n".join(lines)
    assert text.startswith("VERIFIED  ")
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
        lambda: pytest.fail("verify must not start a mutation-enabled MCP transport"),
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
        lambda: pytest.fail("verify must not start a mutation-enabled MCP transport"),
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
