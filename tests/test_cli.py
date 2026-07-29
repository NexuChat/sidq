from __future__ import annotations

from pathlib import Path

import pytest

from sidq import cli
from sidq.models import Evidence, Verdict


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
    assert (
        capsysbinary.readouterr().out
        == b'{"commit_sha":"","decision":"PASS","findings":[],"policy_hash":"policy","reason_code":null,"touched":[]}\n'
    )


def test_explain_known_and_unknown_rules(capsys) -> None:
    assert cli.main(["explain", "unknown_field"]) == 0
    assert "Referenced field" in capsys.readouterr().out
    assert cli.main(["explain", "not_a_rule"]) == 2


def test_evidence_is_enriched_with_a_dataset_deep_link() -> None:
    evidence = Evidence(
        "unknown_field",
        "urn:li:dataset:(urn:li:dataPlatform:dbt,warehouse.customers,PROD)#email",
        {},
    )

    enriched = cli._with_graph_links([evidence])

    assert enriched[0].graph_links == (
        "http://localhost:9002/dataset/urn%3Ali%3Adataset%3A%28urn%3Ali%3AdataPlatform%3Adbt%2Cwarehouse.customers%2CPROD%29",
    )


def test_commit_sha_resolves_the_right_hand_ref_without_running_git(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    ref = git_dir / "refs" / "heads" / "reviewed"
    ref.parent.mkdir(parents=True)
    ref.write_text("a" * 40 + "\n", encoding="utf-8")

    assert cli.commit_sha_for_ref("base..reviewed", repo_root=tmp_path) == "a" * 40
