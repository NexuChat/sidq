"""The judge-facing agent commands, tested as complete CLI journeys."""

from __future__ import annotations

import json
import runpy
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from sidq import cli
from sidq.gates.self_contradiction import (
    CatalogEntity,
    CatalogField,
    CatalogSnapshot,
    LineageEdge,
)
from sidq.graph.client import DatasetInfo, SchemaField
from sidq.mcp_server import server as mcp_server
from sidq.models import Evidence, Verdict
from sidq.receipt.write import RECEIPT_TOOLS
from sidq.repair import REPAIR_TOOLS

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
    def __init__(self, allowed_tools: frozenset[str] | None = None) -> None:
        self.allowed_tools = allowed_tools
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

    def connect(dsn: str) -> _Connection:
        state["dsn"] = dsn
        return connection

    monkeypatch.setattr(psycopg, "connect", connect)

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


def test_claims_reads_source_from_environment_when_option_is_absent(
    monkeypatch, capsys
) -> None:
    state = _wire_claims(monkeypatch, decision="PASS")
    monkeypatch.setenv("CLAIMS_SOURCE", "postgresql://environment-secret")

    assert cli.main(["claims", URN, "--json"]) == 0

    assert state["dsn"] == "postgresql://environment-secret"
    assert state["connection"].read_only is True
    assert "environment-secret" not in capsys.readouterr().err


def test_claims_fails_closed_without_a_source_and_does_not_echo_secrets(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CLAIMS_SOURCE", raising=False)

    assert cli.main(["claims", URN]) == 2

    captured = capsys.readouterr()
    assert "--source" in captured.err
    assert "CLAIMS_SOURCE" in captured.err
    assert URN not in captured.err


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
        "write_assertions": False,
        "as_json": False,
        "apply": False,
        "policy": None,
        "worker_id": "alpha",
        "swarm_run": "run-1",
        "lineage_budget": None,
        "urn": URN,
        "max_age_days": 7,
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

    def caller(allowed_tools: frozenset[str]) -> _Closable:
        created = _Closable(allowed_tools)
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
    monkeypatch.setattr(
        cli,
        "write_receipts",
        lambda receipts, transport: [SimpleNamespace(written=True)],
    )
    monkeypatch.setattr(cli, "render_writeback", lambda outcomes: ["receipt written"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)

    code = cli._audit(_arguments(resume=True, write_receipts=True))

    captured = capsys.readouterr()
    assert code == 1
    assert "re-examining everything" in captured.err
    assert "audit http://datahub" in captured.out
    assert "receipt written" in captured.out
    assert len(callers) == 2 and all(item.closed for item in callers)
    # Both receipt transports are opened with receipt privileges only.
    assert [item.allowed_tools for item in callers] == [RECEIPT_TOOLS] * 2


def test_audit_write_failure_exits_nonzero_without_clean_success(
    monkeypatch, capsys
) -> None:
    result = SimpleNamespace(
        findings=(),
        summary=lambda: {"examined": 1, "findings": 0},
    )
    transport = _Closable()
    failed = SimpleNamespace(written=False, detail="PermissionError")

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["audit clean"])
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
    monkeypatch.setattr(cli, "receipts_for", lambda run, *, commit_sha: ["receipt"])
    monkeypatch.setattr(cli, "write_receipts", lambda receipts, caller: [failed])
    monkeypatch.setattr(
        cli,
        "render_writeback",
        lambda outcomes: ["receipts written  0 of 1", "write failures    1"],
    )
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)

    code = cli._audit(_arguments(write_receipts=True))

    output = capsys.readouterr().out
    assert code == 1
    assert "receipts written  0 of 1" in output
    assert "write failures    1" in output
    assert "receipts written  1 of 1" not in output
    assert transport.closed


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


def test_audit_json_names_every_rollback_incomplete_write(
    monkeypatch, capsysbinary
) -> None:
    result = SimpleNamespace(
        findings=(),
        summary=lambda: {"examined": 6, "findings": 0},
    )
    transport = _Closable()
    failures = [
        SimpleNamespace(
            urn=f"{URN}-asset-{index}",
            verdict="PASS",
            written=False,
            detail=(
                "write_unconfirmed; rollback_incomplete: "
                "state_conflict: concurrent managed receipt detected"
            ),
        )
        for index in reversed(range(6))
    ]

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["unused"])
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
    monkeypatch.setattr(
        cli,
        "receipts_for",
        lambda run, *, commit_sha: [f"receipt-{index}" for index in range(6)],
    )
    monkeypatch.setattr(cli, "write_receipts", lambda receipts, caller: failures)
    monkeypatch.setattr(cli, "render_writeback", lambda outcomes: ["unused"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)

    assert cli._audit(_arguments(write_receipts=True, as_json=True)) == 1

    expected = {
        "examined": 6,
        "findings": 0,
        "writes": {
            "attempted": 6,
            "written": 0,
            "failed": 6,
            "failures": [
                {
                    "urn": f"{URN}-asset-{index}",
                    "verdict": "PASS",
                    "detail": (
                        "write_unconfirmed; rollback_incomplete: "
                        "state_conflict: concurrent managed receipt detected"
                    ),
                }
                for index in range(6)
            ],
        },
    }
    output = capsysbinary.readouterr().out
    assert json.loads(output) == expected
    assert output == (
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    assert transport.closed


@pytest.mark.parametrize(("eligible", "written"), ((1, 1), (0, 0)))
def test_audit_json_reports_success_and_zero_eligible_writes(
    monkeypatch, capsysbinary, eligible: int, written: int
) -> None:
    result = SimpleNamespace(
        findings=(),
        summary=lambda: {"examined": 1, "findings": 0},
    )
    transport = _Closable()
    receipts = ["receipt"] if eligible else []
    outcomes = (
        [SimpleNamespace(urn=URN, verdict="PASS", written=True, detail="")]
        if written
        else []
    )

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    def write(supplied, caller):
        assert supplied == receipts
        assert caller is transport
        return outcomes

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["unused"])
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
    monkeypatch.setattr(cli, "receipts_for", lambda run, *, commit_sha: receipts)
    monkeypatch.setattr(cli, "write_receipts", write)
    monkeypatch.setattr(cli, "render_writeback", lambda supplied: ["unused"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)
    assertion_calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "emit_assertions",
        lambda *args, **kwargs: assertion_calls.append((args, kwargs)),
    )

    assert cli._audit(_arguments(write_receipts=True, as_json=True)) == 0
    assert not assertion_calls
    assert json.loads(capsysbinary.readouterr().out) == {
        "examined": 1,
        "findings": 0,
        "writes": {
            "attempted": eligible,
            "written": written,
            "failed": 0,
            "failures": [],
        },
    }
    assert transport.closed


def test_native_assertions_are_refused_without_receipt_writeback(
    monkeypatch, capsys
) -> None:
    """An assertion mirrors a written receipt, so alone it would mirror nothing.

    Refused before the catalog is read, so an operator who forgot the second
    flag spends no budget and gets no half-run.
    """
    monkeypatch.setattr(
        cli, "_read_snapshot", lambda arguments: pytest.fail("catalog was read")
    )
    monkeypatch.setattr(
        cli, "emit_assertions", lambda *args, **kwargs: pytest.fail("assertions ran")
    )

    assert cli._audit(_arguments(write_assertions=True)) == 2
    assert "--write-assertions requires --write-receipts" in capsys.readouterr().err


def test_a_missing_sdk_refuses_before_the_audit_spends_anything(
    monkeypatch, capsys
) -> None:
    """The mirror's requirement is knowable up front, so it is checked up front.

    Learning it at emission time would mean a spent budget and receipts already
    written, for a refusal that could have opened the run. Nothing is read and
    nothing is written here.
    """
    from sidq.receipt.assertion import DataHubSDKUnavailable

    def unavailable() -> None:
        raise DataHubSDKUnavailable("acryl-datahub pins pydantic below 2.12")

    monkeypatch.setattr(cli, "require_sdk", unavailable)
    monkeypatch.setattr(
        cli, "_read_snapshot", lambda arguments: pytest.fail("catalog was read")
    )
    monkeypatch.setattr(
        cli, "write_receipts", lambda *a, **k: pytest.fail("receipts were written")
    )

    code = cli._audit(_arguments(write_receipts=True, write_assertions=True))

    assert code == 2
    assert "pydantic" in capsys.readouterr().err


def test_native_assertions_mirror_only_the_receipts_the_catalog_accepted(
    monkeypatch, capsysbinary
) -> None:
    """A rejected receipt is not evidence, so it must not become an assertion."""
    result = SimpleNamespace(findings=(), summary=lambda: {"examined": 2})
    written = SimpleNamespace(urn=URN, verdict="PASS", written=True, detail="")
    rejected = SimpleNamespace(
        urn=URN + ".other", verdict="PASS", written=False, detail="Timeout"
    )
    calls: list[tuple[Any, Any]] = []

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    def emit(receipts):
        calls.append(list(receipts))
        return {"created": ("a",), "existing": (), "runs": ("r1", "r2"), "retired": ()}

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["unused"])
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: _Closable()
    )
    monkeypatch.setattr(
        cli, "receipts_for", lambda run, *, commit_sha: ["kept", "dropped"]
    )
    monkeypatch.setattr(
        cli, "write_receipts", lambda supplied, caller: [written, rejected]
    )
    monkeypatch.setattr(cli, "render_writeback", lambda supplied: ["unused"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)
    monkeypatch.setattr(cli, "emit_assertions", emit)
    # This exercises emission, not the environment; the SDK precondition
    # has its own test.
    monkeypatch.setattr(cli, "require_sdk", lambda: None)

    # 1, because a receipt the catalog rejected still fails the run.
    assert (
        cli._audit(_arguments(write_receipts=True, write_assertions=True, as_json=True))
        == 1
    )
    # No GMS url from --server: under --via-mcp that flag is a display label,
    # and passing it could target a different catalog than the receipts.
    assert calls == [["kept"]]
    assert json.loads(capsysbinary.readouterr().out)["assertions"] == {
        # `eligible` separates "nothing to mirror" from "mirrored nothing
        # because the catalog rejected every receipt".
        "eligible": 1,
        "created": 1,
        "existing": 0,
        "runs": 2,
        "retired": 0,
        "failed": False,
    }


def test_a_failed_assertion_write_is_reported_without_counts_and_does_not_pass(
    monkeypatch, capsysbinary
) -> None:
    """Emission raises on the first bad proposal, so earlier ones may have landed.

    Reporting zero created would claim the catalog is untouched when it may not
    be, so the failure carries no counts at all.
    """
    result = SimpleNamespace(findings=(), summary=lambda: {"examined": 1})
    outcome = SimpleNamespace(urn=URN, verdict="PASS", written=True, detail="")

    class _Auditor:
        def __init__(self, snapshot, *, budget, prior) -> None:
            pass

        def run(self):
            return result

    def emit(receipts):
        raise ConnectionError("gms refused the proposal")

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "render", lambda run, *, catalog: ["unused"])
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: _Closable()
    )
    monkeypatch.setattr(cli, "receipts_for", lambda run, *, commit_sha: ["kept"])
    monkeypatch.setattr(cli, "write_receipts", lambda supplied, caller: [outcome])
    monkeypatch.setattr(cli, "render_writeback", lambda supplied: ["unused"])
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "b" * 40)
    monkeypatch.setattr(cli, "emit_assertions", emit)
    # This exercises emission, not the environment; the SDK precondition
    # has its own test.
    monkeypatch.setattr(cli, "require_sdk", lambda: None)

    assert (
        cli._audit(_arguments(write_receipts=True, write_assertions=True, as_json=True))
        == 1
    )
    captured = capsysbinary.readouterr()
    assert json.loads(captured.out)["assertions"] == {"failed": True}
    assert "could not write native assertions" in captured.err.decode()


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
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
    monkeypatch.setattr(
        cli,
        "apply_repairs",
        lambda supplied, caller, *, dry_run: (
            [
                SimpleNamespace(
                    applied=True,
                    closed=True,
                    unresolved=(),
                    collateral=(),
                    proposal=SimpleNamespace(
                        finding_kind=finding.kind, subject=finding.subject
                    ),
                )
            ]
            if not dry_run
            else []
        ),
    )
    monkeypatch.setattr(
        cli,
        "verify_repairs",
        lambda before, outcomes, reader, *, timeout: outcomes,
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
def test_repair_write_failure_is_truthful_and_exits_nonzero(
    monkeypatch, capsysbinary, as_json: bool
) -> None:
    plan = SimpleNamespace(summary=lambda: {"proven": 1, "rejected": 0})
    finding = Evidence("pii_leak_untagged", URN, {})
    proposal = SimpleNamespace(finding_kind=finding.kind, subject=finding.subject)
    result = SimpleNamespace(findings=(finding,))
    failed = SimpleNamespace(
        applied=False,
        closed=False,
        unresolved=(),
        collateral=(),
        detail="PermissionError",
        proposal=proposal,
    )

    class _Auditor:
        def __init__(self, snapshot, *, budget) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "propose_all", lambda findings, snapshot: [])
    monkeypatch.setattr(cli, "prove", lambda snapshot, proposals: plan)
    monkeypatch.setattr(cli, "render_plan", lambda supplied, *, dry_run: ["plan"])
    monkeypatch.setattr(cli, "unfixed", lambda findings, supplied: ())
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", _Closable)
    monkeypatch.setattr(
        cli,
        "apply_repairs",
        lambda supplied, caller, *, dry_run: [failed],
    )
    monkeypatch.setattr(
        cli,
        "render_applied",
        lambda outcomes: ["repairs written   0 of 1", "PermissionError"],
    )

    assert cli._repair(_arguments(apply=True, as_json=as_json)) == 1
    output = capsysbinary.readouterr().out
    if as_json:
        assert output == (
            b'{"proven":1,"rejected":0,"remaining":{"count":1,'
            b'"findings":[{"kind":"pii_leak_untagged","subject":"'
            + URN.encode()
            + b'"}],"kinds":{"pii_leak_untagged":1}},"writes":'
            b'{"applied_unverified":0,"attempted":1,"failed":1,'
            b'"verified":0,"written":0}}\n'
        )
    else:
        assert b"Still standing, unrepaired: 1" in output
        assert b"pii_leak_untagged" in output
        assert b"repairs written   0 of 1" in output
        assert b"PermissionError" in output


def test_repair_acknowledgement_without_live_proof_stays_open_and_nonzero(
    monkeypatch, capsysbinary
) -> None:
    plan = SimpleNamespace(summary=lambda: {"proven": 1, "rejected": 0})
    finding = Evidence("pii_leak_untagged", URN, {})
    proposal = SimpleNamespace(finding_kind=finding.kind, subject=finding.subject)
    result = SimpleNamespace(findings=(finding,))
    acknowledged = SimpleNamespace(
        applied=True,
        verified=False,
        closed=False,
        unresolved=(),
        collateral=(),
        status="applied_unverified",
        detail="applied_unverified: verification timed out",
        proposal=proposal,
    )

    class _Auditor:
        def __init__(self, snapshot, *, budget) -> None:
            pass

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "propose_all", lambda findings, snapshot: [])
    monkeypatch.setattr(cli, "prove", lambda snapshot, proposals: plan)
    monkeypatch.setattr(cli, "render_plan", lambda supplied, *, dry_run: ["plan"])
    monkeypatch.setattr(cli, "unfixed", lambda findings, supplied: ())
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", _Closable)
    monkeypatch.setattr(
        cli,
        "apply_repairs",
        lambda supplied, caller, *, dry_run: [acknowledged],
    )
    monkeypatch.setattr(
        cli,
        "verify_repairs",
        lambda before, outcomes, reader, *, timeout: outcomes,
    )
    monkeypatch.setattr(
        cli,
        "render_applied",
        lambda outcomes: [
            "repairs written   1 of 1",
            "repairs verified  0 of 1",
            "applied_unverified",
        ],
    )

    assert cli._repair(_arguments(apply=True, as_json=True)) == 1
    assert capsysbinary.readouterr().out == (
        b'{"proven":1,"rejected":0,"remaining":{"count":1,'
        b'"findings":[{"kind":"pii_leak_untagged","subject":"'
        + URN.encode()
        + b'"}],"kinds":{"pii_leak_untagged":1}},"writes":'
        b'{"applied_unverified":1,"attempted":1,"failed":0,'
        b'"verified":0,"written":1}}\n'
    )


def _repair_journey() -> tuple[CatalogSnapshot, Evidence]:
    source = CatalogEntity(
        f"{URN}-source",
        "dataset",
        fields=(CatalogField("email", tags=("urn:li:tag:PII",)),),
        owners=("urn:li:corpuser:owner",),
    )
    target = CatalogEntity(
        URN,
        "dataset",
        fields=(CatalogField("email"),),
        owners=("urn:li:corpuser:owner",),
    )
    snapshot = CatalogSnapshot(
        (source, target),
        (LineageEdge(source.urn, "email", target.urn, "email"),),
    )
    finding = Evidence(
        "pii_leak_untagged",
        f"{URN}#email",
        {
            "edge": {"source_urn": source.urn, "source_field": "email"},
            "source_pii_tags": ["urn:li:tag:PII"],
            "target_tags": [],
        },
    )
    return snapshot, finding


def _wire_repair_journey(
    monkeypatch, *, collateral: bool, mutation_changes_state: bool = True
) -> tuple[_Closable, Any]:
    snapshot, finding = _repair_journey()
    if collateral:
        sink = CatalogEntity(
            f"{URN}-sink",
            "dataset",
            fields=(CatalogField("email"),),
            owners=("urn:li:corpuser:owner",),
        )
        snapshot = replace(
            snapshot,
            entities=(*snapshot.entities, sink),
            edges=(*snapshot.edges, LineageEdge(URN, "email", sink.urn, "email")),
        )

    class _MCPBackend:
        def __init__(self) -> None:
            self.entities = {entity.urn: entity for entity in snapshot.entities}
            self.mutations: list[tuple[str, dict[str, Any]]] = []
            self.reads: list[str] = []

        def mutate(self, name: str, arguments: dict[str, Any]) -> None:
            self.mutations.append((name, arguments))
            if not mutation_changes_state:
                return
            assert name == "add_tags"
            tags = {str(tag) for tag in arguments["tag_urns"]}
            for urn, path in zip(arguments["entity_urns"], arguments["column_paths"]):
                entity = self.entities[str(urn)]
                self.entities[str(urn)] = replace(
                    entity,
                    fields=tuple(
                        replace(
                            field,
                            tags=tuple(sorted(set(field.tags) | tags)),
                        )
                        if field.path == path
                        else field
                        for field in entity.fields
                    ),
                )
            if collateral:
                self.entities[URN] = replace(self.entities[URN], owners=())

        def get_dataset(self, urn: str) -> DatasetInfo:
            self.reads.append(urn)
            entity = self.entities[urn]
            return DatasetInfo(
                urn=urn,
                fields=tuple(
                    SchemaField(
                        field.path,
                        "string",
                        True,
                        field.description,
                        tags=field.tags,
                    )
                    for field in entity.fields
                ),
                tags=entity.tags,
                owners=entity.owners,
                deprecated=entity.deprecated,
            )

    backend = _MCPBackend()

    class _Auditor:
        def __init__(self, supplied, *, budget) -> None:
            assert supplied is snapshot

        def run(self):
            return SimpleNamespace(findings=(finding,))

    class _MutationTransport(_Closable):
        def __call__(self, name: str, arguments: dict[str, Any]) -> None:
            backend.mutate(name, arguments)

    class _DirectReader(_Closable):
        def get_dataset(self, urn: str) -> DatasetInfo:
            return backend.get_dataset(urn)

    transport = _MutationTransport()
    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: snapshot)
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)

    def receipt_caller(allowed_tools: frozenset[str]) -> _MutationTransport:
        # `repair --apply` writes on assets Sidq does not own, so it must be
        # handed the proposal tools and never the receipt namespace.
        assert allowed_tools == REPAIR_TOOLS
        transport.allowed_tools = allowed_tools
        return transport

    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", receipt_caller)
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda **kwargs: object())
    monkeypatch.setattr(cli, "MCPGraphClient", lambda caller: _DirectReader())
    return transport, backend


def test_repair_apply_success_is_live_verified_and_exits_zero(
    monkeypatch, capsysbinary
) -> None:
    transport, backend = _wire_repair_journey(monkeypatch, collateral=False)

    assert cli._repair(_arguments(apply=True, as_json=True)) == 0
    assert capsysbinary.readouterr().out == (
        b'{"jointly_verified":true,"proposed":1,"proven":1,'
        b'"proven_by_finding_kind":{"pii_leak_untagged":1},"rejected":0,'
        b'"remaining":{"count":0,"findings":[],"kinds":{}},"writes":'
        b'{"applied_unverified":0,"attempted":1,"failed":0,'
        b'"verified":1,"written":1}}\n'
    )
    assert backend.entities[URN].fields[0].tags == ("urn:li:tag:PII",)
    assert backend.reads == [URN]
    assert transport.closed


def test_repair_acknowledged_no_op_stays_unverified_and_exits_nonzero(
    monkeypatch, capsysbinary
) -> None:
    transport, backend = _wire_repair_journey(
        monkeypatch,
        collateral=False,
        mutation_changes_state=False,
    )

    assert cli._repair(_arguments(apply=True, as_json=True, timeout=0.1)) == 1

    payload = json.loads(capsysbinary.readouterr().out)
    assert payload["remaining"] == {
        "count": 1,
        "findings": [{"kind": "pii_leak_untagged", "subject": f"{URN}#email"}],
        "kinds": {"pii_leak_untagged": 1},
    }
    assert payload["writes"] == {
        "applied_unverified": 1,
        "attempted": 1,
        "failed": 0,
        "verified": 0,
        "written": 1,
    }
    assert backend.entities[URN].fields[0].tags == ()
    assert backend.mutations[0][0] == "add_tags"
    assert backend.reads
    assert transport.closed


@pytest.mark.parametrize("as_json", (False, True))
def test_repair_apply_names_new_collateral_instead_of_the_resolved_original(
    monkeypatch, capsysbinary, as_json: bool
) -> None:
    _wire_repair_journey(monkeypatch, collateral=True)

    assert cli._repair(_arguments(apply=True, as_json=as_json)) == 1
    output = capsysbinary.readouterr().out
    if as_json:
        assert output == (
            b'{"jointly_verified":true,"proposed":1,"proven":1,'
            b'"proven_by_finding_kind":{"pii_leak_untagged":1},"rejected":0,'
            b'"remaining":{"count":1,"findings":[{"kind":"unowned_consumed",'
            b'"subject":"'
            + URN.encode()
            + b'"}],"kinds":{"unowned_consumed":1}},"writes":'
            b'{"applied_unverified":1,"attempted":1,"failed":0,'
            b'"verified":0,"written":1}}\n'
        )
    else:
        assert b"unowned_consumed" in output
        assert URN.encode() in output
        assert b"pii_leak_untagged" not in output.split(b"Still standing")[1]


def test_repair_apply_names_both_unresolved_and_collateral_findings(
    monkeypatch, capsysbinary
) -> None:
    snapshot, original = _repair_journey()
    collateral = Evidence("unowned_consumed", f"{URN}-source", {})
    result = SimpleNamespace(findings=(original,))
    proposal = SimpleNamespace(
        finding_kind=original.kind,
        subject=original.subject,
    )
    outcome = SimpleNamespace(
        applied=True,
        verified=False,
        closed=False,
        unresolved=(
            SimpleNamespace(kind=original.kind, subject=original.subject, detail=""),
        ),
        collateral=(
            SimpleNamespace(
                kind=collateral.kind, subject=collateral.subject, detail=""
            ),
        ),
        detail="required finding remains and collateral was introduced",
        proposal=proposal,
    )

    class _Auditor:
        def __init__(self, supplied, *, budget) -> None:
            assert supplied is snapshot

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: snapshot)
    monkeypatch.setattr(cli, "CatalogAuditor", _Auditor)
    monkeypatch.setattr(cli, "propose_all", lambda findings, supplied: ())
    monkeypatch.setattr(
        cli,
        "prove",
        lambda supplied, proposals: SimpleNamespace(
            summary=lambda: {"proven": 1, "rejected": 0}
        ),
    )
    monkeypatch.setattr(cli, "render_plan", lambda supplied, *, dry_run: ["plan"])
    monkeypatch.setattr(cli, "unfixed", lambda findings, supplied: ())
    monkeypatch.setattr(cli, "StdioMCPReceiptToolCaller", _Closable)
    monkeypatch.setattr(
        cli,
        "apply_repairs",
        lambda supplied, caller, *, dry_run: [outcome],
    )
    monkeypatch.setattr(
        cli,
        "verify_repairs",
        lambda before, outcomes, reader, *, timeout: outcomes,
    )
    monkeypatch.setattr(cli, "render_applied", lambda outcomes: ["unverified"])

    assert cli._repair(_arguments(apply=True, as_json=True)) == 1
    payload = json.loads(capsysbinary.readouterr().out)
    assert payload["remaining"]["kinds"] == {
        "pii_leak_untagged": 1,
        "unowned_consumed": 1,
    }
    assert payload["remaining"]["findings"] == [
        {"kind": "pii_leak_untagged", "subject": original.subject},
        {"kind": "unowned_consumed", "subject": collateral.subject},
    ]


def test_repair_direct_read_is_bounded_even_if_the_transport_hangs(monkeypatch) -> None:
    release = threading.Event()
    closed = threading.Event()

    class _HangingGraph:
        def get_dataset(self, urn: str) -> DatasetInfo:
            release.wait()
            return DatasetInfo(urn=urn)

        def close(self) -> None:
            closed.set()

    graph = _HangingGraph()
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda **kwargs: object())
    monkeypatch.setattr(cli, "MCPGraphClient", lambda caller: graph)
    before = CatalogSnapshot((CatalogEntity(URN, "dataset"),))
    proposal = SimpleNamespace(targets=((URN, None),))

    try:
        with pytest.raises(TimeoutError, match="bounded timeout"):
            cli._read_repair_targets(
                before,
                [proposal],
                server="http://datahub",
                timeout=0.01,
            )
    finally:
        release.set()

    assert closed.wait(timeout=1.0)


@pytest.mark.parametrize("as_json", (False, True))
def test_swarm_worker_closes_transport_and_reports_findings(
    monkeypatch, capsysbinary, as_json: bool
) -> None:
    finding = Evidence("unowned_consumed", URN, {})
    result = SimpleNamespace(
        findings=(finding,),
        write_failures=(),
        summary=lambda: {"worker": "alpha", "findings": 1},
    )
    transport = _Closable()
    seen: dict[str, Any] = {}

    class _Worker:
        def __init__(self, snapshot, **kwargs) -> None:
            seen.update(kwargs)

        def run(self):
            return result

    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
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


@pytest.mark.parametrize("as_json", (False, True))
def test_swarm_write_failures_are_rendered_and_exit_nonzero_without_findings(
    monkeypatch, capsysbinary, as_json: bool
) -> None:
    transport = _Closable()
    result = SimpleNamespace(
        findings=(),
        write_failures=[(URN, "PermissionError")],
        summary=lambda: {
            "worker_id": "alpha",
            "findings": 0,
            "write_failures": 1,
        },
    )
    monkeypatch.setattr(cli, "_read_snapshot", lambda arguments: object())
    monkeypatch.setattr(
        cli, "StdioMCPReceiptToolCaller", lambda allowed_tools: transport
    )
    monkeypatch.setattr(cli, "commit_sha_for_ref", lambda ref: "a" * 40)
    monkeypatch.setattr(
        cli,
        "SwarmWorker",
        lambda *args, **kwargs: SimpleNamespace(run=lambda: result),
    )
    monkeypatch.setattr(
        cli,
        "render_worker",
        lambda run: ["findings          0", "write failures    1"],
    )

    assert cli._swarm(_arguments(as_json=as_json)) == 1
    output = capsysbinary.readouterr().out
    if as_json:
        assert json.loads(output)["write_failures"] == 1
    else:
        assert b"findings          0" in output
        assert b"write failures    1" in output
    assert transport.closed


def test_swarm_ledger_is_read_from_datahub_and_transport_errors_fail_closed(
    monkeypatch, capsys
) -> None:
    snapshot = SimpleNamespace(entities=[SimpleNamespace(urn=URN)])
    callers: list[_Closable] = []

    def caller() -> _Closable:
        """The read-only graph transport, which takes no allowlist."""
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
        lambda *args, **kwargs: pytest.fail(
            "ledger reads must not enable mutation tools"
        ),
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


@pytest.mark.parametrize(
    ("status", "code", "headline"),
    (
        ({"verdict": "PASS", "stale": False}, 0, "CURRENT RECEIPT · PASS · CONTINUE"),
        (
            {"verdict": "WARN", "stale": False},
            0,
            "CURRENT RECEIPT · WARN · REVIEW_OR_ESCALATE",
        ),
        ({"verdict": "BLOCK", "stale": False}, 1, "CURRENT RECEIPT · BLOCK · STOP"),
        ({"verdict": None}, 1, "NOT VERIFIED"),
        ({"verdict": "PASS", "stale": True}, 1, "NOT VERIFIED"),
        # No freshness marker at all: unreadable, not fresh.
        ({"verdict": "PASS"}, 1, "NOT VERIFIED"),
    ),
)
def test_verify_reports_receipt_state_from_a_separate_reader(
    monkeypatch, capsys, status: dict, code: int, headline: str
) -> None:
    """Exit code and headline together, because neither alone is the answer.

    A refusal and an absent receipt share exit 1 — a script's only safe move is
    to stop in both cases — so the printed headline is what distinguishes "we
    checked and refused" from "nobody checked".
    """
    transport = _Closable()
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: transport)
    monkeypatch.setattr(cli, "get_verification_status", lambda *args, **kwargs: status)

    assert cli._verify(_arguments()) == code
    assert headline in capsys.readouterr().out
    assert transport.closed


def test_verify_json_reports_each_axis_separately(monkeypatch, capsysbinary) -> None:
    """No single boolean to misread: state, action, and coverage are all emitted."""
    transport = _Closable()
    status = {"verdict": "BLOCK", "stale": False, "policy_hash": "policy"}
    monkeypatch.setattr(cli, "StdioMCPToolCaller", lambda: transport)
    monkeypatch.setattr(
        cli,
        "StdioMCPReceiptToolCaller",
        lambda *args, **kwargs: pytest.fail(
            "receipt reads must not enable mutation tools"
        ),
    )
    monkeypatch.setattr(cli, "get_verification_status", lambda *args, **kwargs: status)

    assert cli._verify(_arguments(as_json=True)) == 1
    assert json.loads(capsysbinary.readouterr().out) == {
        "policy_hash": "policy",
        "stale": False,
        "verdict": "BLOCK",
        "receipt_state": "CURRENT",
        "action": "STOP",
        "covers_asset": True,
        "judgment": "receipt records a current refusal (BLOCK); stop",
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
