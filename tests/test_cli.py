from __future__ import annotations

import pytest

from sidq import cli
from sidq.models import Verdict


def _verdict(decision: str) -> Verdict:
    return Verdict(decision, None, (), (), "", "policy")


@pytest.mark.parametrize(("decision", "code"), [("PASS", 0), ("WARN", 1), ("BLOCK", 2)])
def test_check_exit_codes(monkeypatch, decision: str, code: int, capsys) -> None:
    monkeypatch.setattr(cli, "check", lambda *args, **kwargs: _verdict(decision))

    assert cli.main(["check", "--file", "model.sql"]) == code
    assert f"Sidq: {decision}" in capsys.readouterr().out


def test_json_uses_canonical_artifact(monkeypatch, capsysbinary) -> None:
    monkeypatch.setattr(cli, "check", lambda *args, **kwargs: _verdict("PASS"))

    assert cli.main(["check", "--file", "model.sql", "--json"]) == 0
    assert capsysbinary.readouterr().out == b'{"commit_sha":"","decision":"PASS","findings":[],"policy_hash":"policy","reason_code":null,"touched":[]}\n'


def test_explain_known_and_unknown_rules(capsys) -> None:
    assert cli.main(["explain", "unknown_field"]) == 0
    assert "Referenced field" in capsys.readouterr().out
    assert cli.main(["explain", "not_a_rule"]) == 2
