from __future__ import annotations

import asyncio
import json
import re
import sys
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Self

import anyio
import mcp
import mcp.client.stdio
import pytest

from sidq.models import Evidence, Finding, Verdict
from sidq.policy.engine import PolicyEngine
from sidq.receipt.bootstrap import (
    PROPERTY_DEFINITIONS,
    definitions,
    ensure_sidq_properties,
    property_urn,
)
from sidq.receipt.build import build_receipt
from sidq.receipt.read import get_verification_status
from sidq.receipt.write import (
    StdioMCPReceiptToolCaller,
    _document_reference,
    write_receipt,
)

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,sidq.receipt.test,DEV)"


def _verdict(decision: str = "PASS") -> Verdict:
    finding = Finding(
        "schema.required",
        "block",
        "A required field is missing.",
        (Evidence("schema", URN, {"field": "email"}),),
    )
    return Verdict(
        decision,
        "MISSING_FIELD" if decision == "BLOCK" else None,
        (finding,),
        (),
        "a" * 40,
        "sha256:policy",
    )


def test_build_receipt_is_deterministic_and_records_block() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    assert receipt.verdict == "BLOCK"
    assert receipt.rules_fired == ("schema.required",)
    assert receipt.checked_at == "2026-08-02T11:04:00Z"
    assert receipt.structured_property_values()[
        "urn:li:structuredProperty:sidq.reason_code"
    ] == ["MISSING_FIELD"]


def test_write_uses_only_the_three_official_mcp_mutation_tools() -> None:
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        return {"success": True, "urn": "urn:li:document:sidq-receipt"}

    written = write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "save_document",
        "add_structured_properties",
        "add_tags",
    ]
    assert calls[1][1]["property_values"][
        "urn:li:structuredProperty:sidq.evidence_url"
    ] == ["urn:li:document:sidq-receipt"]
    assert calls[2][1]["tag_urns"] == ["urn:li:tag:sidq:verified"]
    assert written["receipt"]["evidence_url"] == "urn:li:document:sidq-receipt"


def test_block_receipt_is_written_and_uses_the_blocked_badge() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        return {"success": True, "urn": "urn:li:document:sidq-blocked"}

    write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "save_document",
        "add_structured_properties",
        "add_tags",
    ]
    assert calls[-1][1]["tag_urns"] == ["urn:li:tag:sidq:blocked"]


def test_read_computes_schema_policy_and_age_staleness() -> None:
    entity = {
        "urn": URN,
        "schemaMetadata": {"lastModified": {"time": 1785668700000}},
        "structuredProperties": {
            "properties": [
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.verdict"
                    },
                    "values": [{"stringValue": "PASS"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.reason_code"
                    },
                    "values": [{"stringValue": ""}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.commit_sha"
                    },
                    "values": [{"stringValue": "abc"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.checked_at"
                    },
                    "values": [{"stringValue": "2026-08-02T11:04:00Z"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.policy_hash"
                    },
                    "values": [{"stringValue": "sha256:old"}],
                },
                {
                    "structuredProperty": {
                        "urn": "urn:li:structuredProperty:sidq.rules_fired"
                    },
                    "values": [{"stringValue": "schema.required"}],
                },
            ]
        },
    }
    caller = lambda name, arguments: [entity]

    schema_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:old",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    assert schema_stale["stale"] is True
    assert (
        schema_stale["stale_reason"]
        == "asset schema changed after the last verification"
    )

    entity["schemaMetadata"]["lastModified"]["time"] = 1785668400000
    policy_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:new",
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    assert (
        policy_stale["stale_reason"]
        == "policy hash changed since the last verification"
    )

    age_stale = get_verification_status(
        URN,
        caller,
        current_policy_hash="sha256:old",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        max_age=timedelta(days=7),
    )
    assert age_stale["stale_reason"] == "receipt exceeded the maximum verification age"


class _BootstrapValue:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _BootstrapGraph:
    def __init__(self) -> None:
        self.aspects: dict[str, object] = {}
        self.closed = False

    def get_aspect(self, urn: str, aspect_type: object) -> object | None:
        return self.aspects.get(urn)

    def emit_mcp(self, mcp: object) -> None:
        self.aspects[mcp.entityUrn] = mcp.aspect

    def close(self) -> None:
        self.closed = True


def _install_fake_datahub_sdk(monkeypatch, graph: _BootstrapGraph) -> list[object]:
    """Expose only the SDK seam bootstrap consumes, with no optional install."""
    configs: list[object] = []

    class _Cardinality:
        SINGLE = "single"
        MULTIPLE = "multiple"

    class _Urn:
        make_data_type_urn = staticmethod(lambda name: f"type:{name}")
        make_entity_type_urn = staticmethod(lambda name: f"entity:{name}")

    modules = {
        "datahub": ModuleType("datahub"),
        "datahub.emitter": ModuleType("datahub.emitter"),
        "datahub.emitter.mcp": ModuleType("datahub.emitter.mcp"),
        "datahub.ingestion": ModuleType("datahub.ingestion"),
        "datahub.ingestion.graph": ModuleType("datahub.ingestion.graph"),
        "datahub.ingestion.graph.client": ModuleType("datahub.ingestion.graph.client"),
        "datahub.ingestion.graph.config": ModuleType("datahub.ingestion.graph.config"),
        "datahub.metadata": ModuleType("datahub.metadata"),
        "datahub.metadata.schema_classes": ModuleType(
            "datahub.metadata.schema_classes"
        ),
        "datahub.metadata.urns": ModuleType("datahub.metadata.urns"),
    }
    for package in (
        "datahub",
        "datahub.emitter",
        "datahub.ingestion",
        "datahub.ingestion.graph",
        "datahub.metadata",
    ):
        modules[package].__path__ = []  # type: ignore[attr-defined]

    modules["datahub.emitter.mcp"].MetadataChangeProposalWrapper = _BootstrapValue
    schema = modules["datahub.metadata.schema_classes"]
    schema.PropertyCardinalityClass = _Cardinality
    schema.PropertyValueClass = _BootstrapValue
    schema.StructuredPropertyDefinitionClass = _BootstrapValue
    schema.TagPropertiesClass = _BootstrapValue
    modules["datahub.metadata.urns"].Urn = _Urn
    modules["datahub.ingestion.graph.config"].DatahubClientConfig = _BootstrapValue

    def datahub_graph(config: object) -> _BootstrapGraph:
        configs.append(config)
        return graph

    modules["datahub.ingestion.graph.client"].DataHubGraph = datahub_graph
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return configs


def test_bootstrap_is_idempotent_with_a_graph_double(monkeypatch) -> None:
    graph = _BootstrapGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)
    first = ensure_sidq_properties(graph)
    second = ensure_sidq_properties(graph)

    assert len(first["created"]) == len(PROPERTY_DEFINITIONS) + 2
    assert not second["created"]
    rules = graph.aspects["urn:li:structuredProperty:sidq.rules_fired"]
    assert rules.cardinality == "multiple"
    verdict = graph.aspects["urn:li:structuredProperty:sidq.verdict"]
    assert [value.value for value in verdict.allowedValues] == ["PASS", "WARN", "BLOCK"]


def test_bootstrap_owns_and_closes_the_graph_it_constructs(monkeypatch) -> None:
    graph = _BootstrapGraph()
    configs = _install_fake_datahub_sdk(monkeypatch, graph)

    result = ensure_sidq_properties(gms_url="https://catalog.example.test")

    assert result["created"]
    assert configs[0].server == "https://catalog.example.test"
    assert graph.closed


def test_bootstrap_uses_the_environment_and_rejects_foreign_properties(
    monkeypatch,
) -> None:
    graph = _BootstrapGraph()
    configs = _install_fake_datahub_sdk(monkeypatch, graph)
    monkeypatch.setenv("DATAHUB_GMS_URL", "https://catalog.env.test")

    ensure_sidq_properties()

    assert configs[0].server == "https://catalog.env.test"
    assert property_urn("verdict") == "urn:li:structuredProperty:sidq.verdict"
    with pytest.raises(ValueError, match="unknown Sidq structured property"):
        property_urn("foreign")
    assert tuple(definitions()) == PROPERTY_DEFINITIONS


def test_write_receipt_propagates_write_rejection_without_claiming_success() -> None:
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[str] = []

    def rejected(name: str, arguments: object) -> object:
        calls.append(name)
        raise PermissionError("mutation disabled")

    with pytest.raises(PermissionError, match="mutation disabled"):
        write_receipt(receipt, rejected)

    assert calls == ["save_document"]
    assert _document_reference({"urn": 42}) == ""
    assert _document_reference("not-a-response") == ""


class _ImmediateThread:
    def __init__(self, *, target, **kwargs) -> None:
        self.target = target
        self.joined = False

    def start(self) -> None:
        self.target()

    def join(self, timeout: float) -> None:
        self.joined = True


class _ImmediateQueue:
    def put(self, item: object) -> None:
        if isinstance(item, tuple):
            item[2].set_result({"ok": True})


def test_receipt_stdio_caller_returns_tool_result_and_closes(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller()
    caller._requests = _ImmediateQueue()  # type: ignore[assignment]
    monkeypatch.setattr("sidq.receipt.write.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(caller, "_run", lambda: caller._startup.set_result(None))

    assert caller("add_tags", {}) == {"ok": True}
    thread = caller._thread
    assert thread is not None
    caller.close()
    assert thread.joined is True
    assert caller._thread is None


def test_receipt_stdio_startup_timeout_is_relayed(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller()

    class _AnyIO:
        @staticmethod
        def run(function) -> None:
            raise TimeoutError("MCP startup timed out")

    monkeypatch.setitem(sys.modules, "anyio", _AnyIO)
    caller._run()

    with pytest.raises(TimeoutError, match="startup timed out"):
        caller._startup.result()


class _AsyncContext:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *args: object) -> None:
        return None


class _MCPResponse:
    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [type("Text", (), {"type": "text", "text": text})()]
        self.is_error = is_error
        self.structured_content = None


@pytest.mark.parametrize(
    ("response", "exception"),
    [
        (_MCPResponse("write rejected", is_error=True), RuntimeError),
        (_MCPResponse("not-json"), json.JSONDecodeError),
    ],
)
def test_receipt_stdio_caller_rejects_malformed_or_error_mcp_responses(
    monkeypatch, response: _MCPResponse, exception: type[Exception]
) -> None:
    caller = StdioMCPReceiptToolCaller()
    result: Future[object] = Future()
    requests = [("add_tags", {}, result), None]

    async def next_request(function):
        return requests.pop(0)

    class _Session:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> _MCPResponse:
            return response

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(anyio.to_thread, "run_sync", next_request)
    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(
        mcp.client.stdio,
        "stdio_client",
        lambda parameters: _AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        mcp.client.stdio, "StdioServerParameters", lambda **kwargs: kwargs
    )

    asyncio.run(caller._serve())

    with pytest.raises(exception):
        result.result()


def test_a_stale_transcript_hash_must_be_labelled_historical() -> None:
    """`examples/02` publishes a dated live run whose policy_hash drifts.

    The scripts compute the hash from the shipped policy, so every policy edit
    makes the recorded transcript stale. Rewriting a recorded live run to match
    today's code would destroy what it proves, so the transcript stays verbatim —
    but it must never read as current. If it is stale, the README has to say so.
    """
    readme = (
        Path(__file__).parents[1] / "examples" / "02-receipt-consumed" / "README.md"
    )
    text = readme.read_text(encoding="utf-8")
    transcript_hashes = set(re.findall(r'"policy_hash": "([0-9a-f]{64})"', text))
    current = PolicyEngine().decide((), commit_sha="receipt-proof-commit").policy_hash

    if transcript_hashes and transcript_hashes != {current}:
        assert "historical" in text.lower(), (
            "the transcript policy_hash no longer matches the shipped policy, so "
            "the README must label it historical instead of implying it is current"
        )
