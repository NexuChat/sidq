"""The judge-facing agent commands, tested as complete CLI journeys."""

from __future__ import annotations

import json
import runpy
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from sidq import cli
from sidq.mcp_server import server as mcp_server
from sidq.models import Evidence, Verdict

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.public.orders,PROD)"


def test_python_module_entrypoint_returns_cli_status(monkeypatch) -> None:
    """``python -m sidq`` must preserve the CLI's process status."""

    monkeypatch.setattr(cli, "main", lambda: 7)
    with pytest.raises(SystemExit, match="7"):
        runpy.run_module("sidq.__main__", run_name="__main__")


def test_mcp_module_entrypoint_returns_server_status(monkeypatch) -> None:
    """``python -m sidq.mcp_server`` must invoke the stdio server."""

    monkeypatch.setattr(mcp_server, "main", lambda: 0)
    with pytest.raises(SystemExit, match="0"):
        runpy.run_module("sidq.mcp_server.__main__", run_name="__main__")


class _Closable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Connection:
    read_only = False


class _Run:
    dropped: ClassVar[list[Any]] = []

    def __init__(self) -> None:
        claim = SimpleNamespace(
            column="order_id",
            origin="model",
            source_sentence="A unique key for each row.",
        )
        verification = SimpleNamespace(status="holds", violating_row_count=0)
        self.admitted = [
            SimpleNamespace(urn=URN, claim=claim, verification=verification)
        ]

    def evidence(self) -> tuple[Evidence, ...]:
        return (Evidence("doc_claim_holds", f"{URN}#order_id", {}),)

    def summary(self) -> dict[str, object]:
        return {"documented_fields": 1, "claims_proposed": 1}


def _wire_claims(monkeypatch, *, decision: str = "WARN") -> dict[str, Any]:
    """Replace transports, not CLI behavior, so parsing and reporting stay real."""
    import psycopg

    from sidq.claims import attest, verify
    from sidq.graph import live_source

    state: dict[str, Any] = {}
    graph = _Closable()
    connection = _Connection()

    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: object())
    monkeypatch.setattr(cli, "MCPGraphClient", lambda _: graph)
    monkeypatch.setattr(attest, "datasets_from", lambda source, urns: [object()])
    monkeypatch.setattr(psycopg, "connect", lambda dsn: connection)

    class _LiveSource:
        def __init__(self, connect) -> None:
            state["live_connection"] = connect()

    class _Verifier:
        def __init__(self, source, connect) -> None:
            state["verifier_source"] = source
            state["verifier_connect"] = connect

    class _Attester:
        def __init__(self, verifier, *, extra, min_confidence) -> None:
            state["extra"] = extra
            state["min_confidence"] = min_confidence

        def run(self, datasets, *, budget):
            state["datasets"] = datasets
            state["budget"] = budget
            return _Run()

    class _Policy:
        def __init__(self, policy) -> None:
            state["policy"] = policy

        def decide(self, evidence, *, commit_sha) -> Verdict:
            state["evidence"] = evidence
            state["commit_sha"] = commit_sha
            return Verdict(decision, None, (), (), commit_sha, "policy")

    monkeypatch.setattr(live_source, "PostgresLiveSourceClient", _LiveSource)
    monkeypatch.setattr(verify, "ClaimVerifier", _Verifier)
    monkeypatch.setattr(attest, "DocumentationAttester", _Attester)
    monkeypatch.setattr(cli, "PolicyEngine", _Policy)
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "a" * 40)
    state["graph"] = graph
    state["connection"] = connection
    return state


def test_claims_reader_run_reports_the_exact_reader_identity(
    monkeypatch, capsys
) -> None:
    from sidq.claims import attest, reader

    state = _wire_claims(monkeypatch)
    identity = {
        "model": "reader-model",
        "revision": "12345678",
        "head_sha256": "abcdef12",
        "threshold": 0.51,
    }

    class _Reader:
        def __init__(self) -> None:
            self.identity = identity

    monkeypatch.setattr(reader, "EmbeddingClaimReader", _Reader)
    monkeypatch.setattr(
        attest,
        "render",
        lambda run, decision, *, reader_identity: [
            f"{decision} {reader_identity['model']} {reader_identity['head_sha256']}"
        ],
    )

    code = cli.main(
        [
            "claims",
            URN,
            "--source",
            "postgresql://warehouse",
            "--reader",
            "--min-confidence",
            "0.7",
            "--budget",
            "9",
        ]
    )

    assert code == 0
    assert "WARN reader-model abcdef12" in capsys.readouterr().out
    assert state["graph"].closed
    assert state["connection"].read_only is True
    assert state["min_confidence"] == 0.7
    assert state["budget"] == 9
    assert state["commit_sha"] == "a" * 40


def test_claims_json_names_the_local_model_that_proposed_work(
    monkeypatch, capsys
) -> None:
    from sidq.claims import extractor

    _wire_claims(monkeypatch, decision="PASS")

    class _Model:
        def __init__(self, model: str) -> None:
            self.model = model

    monkeypatch.setattr(extractor, "ModelExtractor", _Model)

    assert (
        cli.main(
            [
                "claims",
                URN,
                "--source",
                "postgresql://warehouse",
                "--model",
                "local-reader:1",
                "--json",
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document["proposal_reader"] == {
        "kind": "ollama",
        "model": "local-reader:1",
    }
    assert document["findings"][0] == {
        "urn": URN,
        "column": "order_id",
        "origin": "model",
        "sentence": "A unique key for each row.",
        "status": "holds",
        "violating_rows": 0,
    }


@pytest.mark.parametrize("option", ("--reader", "--model"))
def test_claims_falls_back_to_rules_when_an_optional_reader_is_unavailable(
    monkeypatch, capsys, option: str
) -> None:
    from sidq.claims import attest, extractor, reader

    state = _wire_claims(monkeypatch)

    def unavailable(*args, **kwargs):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(reader, "EmbeddingClaimReader", unavailable)
    monkeypatch.setattr(extractor, "ModelExtractor", unavailable)
    monkeypatch.setattr(
        attest,
        "render",
        lambda run, decision, *, reader_identity: [f"{decision} rules only"],
    )
    arguments = ["claims", URN, "--source", "postgresql://warehouse", option]
    if option == "--model":
        arguments.append("missing:1")

    assert cli.main(arguments) == 0

    captured = capsys.readouterr()
    assert "unavailable, rules only" in captured.err
    assert "WARN rules only" in captured.out
    assert state["extra"] is None


def test_claims_refuses_an_empty_catalog_read(monkeypatch, capsys) -> None:
    from sidq.claims import attest

    graph = _Closable()
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: object())
    monkeypatch.setattr(cli, "MCPGraphClient", lambda _: graph)
    monkeypatch.setattr(attest, "datasets_from", lambda source, urns: [])

    assert cli.main(["claims", URN, "--source", "postgresql://warehouse"]) == 2
    assert graph.closed
    assert "none of the named datasets" in capsys.readouterr().err


def test_claims_reader_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "claims",
                URN,
                "--source",
                "postgresql://warehouse",
                "--reader",
                "--model",
            ]
        )


def _arguments(**overrides: Any) -> SimpleNamespace:
    values = {
        "server": "http://datahub",
        "budget": 3,
        "timeout": 1.0,
        "via_mcp": True,
        "resume": False,
        "write_receipts": False,
        "as_json": False,
        "apply": False,
        "policy": None,
        "worker_id": "alpha",
        "swarm_run": "run-1",
        "lineage_budget": None,
        "urn": URN,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mcp_snapshot_closes_its_transport_on_success_and_failure(
    monkeypatch, capsys
) -> None:
    graph = _Closable()
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: object())
    monkeypatch.setattr(cli, "MCPGraphClient", lambda _: graph)
    monkeypatch.setattr(
        cli.CatalogSnapshot,
        "from_mcp",
        lambda source, *, field_lineage_budget: SimpleNamespace(
            source=source, budget=field_lineage_budget
        ),
    )

    snapshot = cli._read_snapshot(_arguments(budget=7))

    assert snapshot.budget == 7
    assert graph.closed

    failed_graph = _Closable()
    monkeypatch.setattr(cli, "MCPGraphClient", lambda _: failed_graph)

    def fail(*args, **kwargs):
        raise RuntimeError("MCP stopped")

    monkeypatch.setattr(cli.CatalogSnapshot, "from_mcp", fail)

    assert cli._read_snapshot(_arguments()) is None
    assert failed_graph.closed
    assert "could not read the catalog over MCP" in capsys.readouterr().err


def test_audit_resumes_writes_receipts_and_names_unreached_failures(
    monkeypatch, capsys
) -> None:
    snapshot = SimpleNamespace(entities=[SimpleNamespace(urn=URN)])
    finding = Evidence("lineage_field_missing", URN, {})
    result = SimpleNamespace(
        findings=(finding,),
        summary=lambda: {"examined": 1, "findings": 1},
    )
    callers: list[_Closable] = []

    def caller() -> _Closable:
        created = _Closable()
        callers.append(created)
        return created

    class _Auditor:
        def __init__(self, supplied, *, budget, prior) -> None:
            assert supplied is snapshot
            assert budget == 3
            assert prior == {}

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: snapshot)
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", caller)
    monkeypatch.setattr(
        cli,
        "recall",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("receipt read failed")
        ),
    )
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: [f"audit {catalog}"])
    monkeypatch.setattr(cli, "receipts_for", lambda run, *, commit_sha: ["receipt"])
    monkeypatch.setattr(cli, "write_receipts", lambda receipts, transport: ["written"])
    monkeypatch.setattr(cli, "render_writeback", lambda outcomes: ["receipt written"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)

    code = cli._audit(_arguments(resume=True, write_receipts=True))

    captured = capsys.readouterr()
    assert code == 1
    assert "re-examining everything" in captured.err
    assert "audit http://datahub" in captured.out
    assert "receipt written" in captured.out
    assert len(callers) == 2 and all(item.closed for item in callers)


def test_audit_json_is_the_canonical_summary(monkeypatch, capsysbinary) -> None:
    result = SimpleNamespace(findings=(), summary=lambda: {"examined": 2})

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["unused"])

    assert cli._audit(_arguments(as_json=True)) == 0
    assert capsysbinary.readouterr().out == b'{"examined":2}\n'


def test_repair_names_what_remains_and_applies_only_the_proven_plan(
    monkeypatch, capsys
) -> None:
    snapshot = object()
    finding = Evidence("orphan_lineage", URN, {})
    result = SimpleNamespace(findings=(finding,))
    plan = SimpleNamespace(summary=lambda: {"proved": 1})
    transport = _Closable()

    class _Auditor:
        def __init__(self, supplied, *, budget) -> None:
            assert supplied is snapshot

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: snapshot)
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "propose_all", lambda findings, supplied: ["proposal"])
    monkeypatch.setattr(cli, "prove", lambda supplied, proposals: plan)
    monkeypatch.setattr(
        cli, "render_plan", lambda supplied, *, dry_run: ["proved plan"]
    )
    monkeypatch.setattr(cli, "unfixed", lambda findings, supplied: (finding,))
    monkeypatch.setitem(cli.UNREPAIRABLE, "orphan_lineage", "needs source authority")
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", lambda: transport)
    monkeypatch.setattr(
        cli,
        "apply_repairs",
        lambda supplied, caller, *, dry_run: ["applied"] if not dry_run else [],
    )
    monkeypatch.setattr(cli, "render_applied", lambda outcomes: ["applied once"])

    assert cli._repair(_arguments(apply=True)) == 1

    output = capsys.readouterr().out
    assert "proved plan" in output
    assert "Still standing, unrepaired: 1" in output
    assert "needs source authority" in output
    assert "applied once" in output
    assert transport.closed


def test_repair_json_returns_zero_when_every_finding_is_closed(
    monkeypatch, capsysbinary
) -> None:
    plan = SimpleNamespace(summary=lambda: {"proved": 2, "rejected": 0})
    result = SimpleNamespace(findings=())

    class _Auditor:
        def __init__(self, snapshot, *, budget) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "propose_all", lambda findings, snapshot: [])
    monkeypatch.setattr(cli, "prove", lambda snapshot, proposals: plan)
    monkeypatch.setattr(cli, "render_plan", lambda supplied, *, dry_run: [])
    monkeypatch.setattr(cli, "unfixed", lambda findings, supplied: ())

    assert cli._repair(_arguments(as_json=True)) == 0
    assert capsysbinary.readouterr().out == b'{"proved":2,"rejected":0}\n'


@pytest.mark.parametrize("as_json", (False, True))
def test_swarm_worker_closes_transport_and_reports_findings(
    monkeypatch, capsysbinary, as_json: bool
) -> None:
    finding = Evidence("unowned_consumed", URN, {})
    result = SimpleNamespace(
        findings=(finding,), summary=lambda: {"worker": "alpha", "findings": 1}
    )
    transport = _Closable()
    seen: dict[str, Any] = {}

    class _Worker:
        def __init__(self, snapshot, **kwargs) -> None:
            seen.update(kwargs)

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", lambda: transport)
    monkeypatch.setattr(cli, "SwarmWorker", _Worker)
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "c" * 40)
    monkeypatch.setattr(cli, "render_worker", lambda supplied: ["worker alpha"])

    assert cli._swarm(_arguments(as_json=as_json, budget=4)) == 1
    assert transport.closed
    assert seen["worker_id"] == "alpha"
    assert seen["swarm_run"] == "run-1"
    assert seen["budget"] == 4
    if as_json:
        assert json.loads(capsysbinary.readouterr().out) == {
            "findings": 1,
            "worker": "alpha",
        }
    else:
        assert b"worker alpha" in capsysbinary.readouterr().out


def test_swarm_ledger_is_read_from_datahub_and_transport_errors_fail_closed(
    monkeypatch, capsys
) -> None:
    snapshot = SimpleNamespace(entities=[SimpleNamespace(urn=URN)])
    callers: list[_Closable] = []

    def caller() -> _Closable:
        created = _Closable()
        callers.append(created)
        return created

    report = SimpleNamespace(
        render=lambda: ["ledger run-1"], summary=lambda: {"covered": 1}
    )
    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: snapshot)
    monkeypatch.setattr(cli, "StdioMCPToolCaller", caller)
    monkeypatch.setattr(
        cli,
        "StdioMCPReceiptToolCaller",
        lambda: pytest.fail("ledger reads must not enable mutation tools"),
    )
    monkeypatch.setattr(
        cli, "get_verification_statuses", lambda urns, transport: {URN: {}}
    )
    monkeypatch.setattr(cli, "observe", lambda urns, statuses, *, swarm_run: report)

    assert cli._swarm_ledger(_arguments()) == 0
    assert "ledger run-1" in capsys.readouterr().out
    assert callers[-1].closed

    monkeypatch.setattr(
        cli,
        "get_verification_statuses",
        lambda urns, transport: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )

    assert cli._swarm_ledger(_arguments()) == 2
    assert "could not read the ledger" in capsys.readouterr().err
    assert callers[-1].closed


@pytest.mark.parametrize(("verified", "code"), ((True, 0), (False, 1)))
def test_verify_reports_receipt_state_from_a_separate_reader(
    monkeypatch, capsys, verified: bool, code: int
) -> None:
    transport = _Closable()
    status = {"verdict": "PASS" if verified else "BLOCK"}
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: transport)
    monkeypatch.setattr(cli, "get_verification_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(cli, "holds", lambda supplied: (verified, "reason"))
    monkeypatch.setattr(
        cli, "render_verification", lambda urn, supplied: [f"verified={verified}"]
    )

    assert cli._verify(_arguments()) == code
    assert f"verified={verified}" in capsys.readouterr().out
    assert transport.closed


def test_verify_json_includes_the_recomputed_status(monkeypatch, capsysbinary) -> None:
    transport = _Closable()
    status = {"verdict": "PASS", "policy_hash": "policy"}
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: transport)
    monkeypatch.setattr(
        cli,
        "StdioMCPReceiptToolCaller",
        lambda: pytest.fail("receipt reads must not enable mutation tools"),
    )
    monkeypatch.setattr(cli, "get_verification_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(cli, "holds", lambda supplied: (True, "fresh"))

    assert cli._verify(_arguments(as_json=True)) == 0
    assert json.loads(capsysbinary.readouterr().out) == {
        "policy_hash": "policy",
        "verdict": "PASS",
        "verified": True,
    }
    assert transport.closed


def test_verify_transport_failure_is_not_reported_as_unverified(
    monkeypatch, capsys
) -> None:
    transport = _Closable()
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: transport)
    monkeypatch.setattr(
        cli,
        "get_verification_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("MCP down")),
    )

    assert cli._verify(_arguments()) == 2
    assert "could not read the receipt" in capsys.readouterr().err
    assert transport.closed
