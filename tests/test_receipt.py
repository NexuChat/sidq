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

from sidq.agent.writeback import render_writeback, write_receipts
from sidq.models import Evidence, Finding, Verdict
from sidq.policy.engine import PolicyEngine
from sidq.receipt.bootstrap import (
    PROPERTY_DEFINITIONS,
    definitions,
    ensure_sidq_properties,
    property_urn,
)
from sidq.receipt.build import build_receipt
from sidq.receipt.read import (
    _without_sidq_receipt_documents,
    get_verification_status,
    get_verification_statuses,
)
from sidq.receipt.write import (
    StdioMCPReceiptToolCaller,
    _document_reference,
    _mcp_subprocess_environment,
    write_receipt,
)
from sidq.serialization import canonical_json

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
        if name == "get_entities":
            return {"entities": [{"urn": URN}]}
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "returned": 0,
                    "hasMore": False,
                    "searchResults": [],
                }
            }
        return {"success": True, "urn": "urn:li:document:sidq-receipt"}

    written = write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "get_entities",
        "get_lineage",
        "get_lineage",
        "save_document",
        "add_structured_properties",
        "add_tags",
    ]
    assert calls[4][1]["property_values"][
        "urn:li:structuredProperty:sidq.evidence_url"
    ] == ["urn:li:document:sidq-receipt"]
    assert calls[4][1]["property_values"][
        "urn:li:structuredProperty:sidq.context_hash"
    ][0].startswith("sha256:")
    assert calls[5][1]["tag_urns"] == ["urn:li:tag:sidq:verified"]
    assert written["receipt"]["evidence_url"] == "urn:li:document:sidq-receipt"


def test_block_receipt_is_written_and_uses_the_blocked_badge() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {"entities": [{"urn": URN}]}
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "returned": 0,
                    "hasMore": False,
                    "searchResults": [],
                }
            }
        return {"success": True, "urn": "urn:li:document:sidq-blocked"}

    write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "get_entities",
        "get_lineage",
        "get_lineage",
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


def test_read_fails_closed_when_metadata_freshness_cannot_be_proved() -> None:
    entity = {
        "urn": URN,
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
                        "urn": "urn:li:structuredProperty:sidq.checked_at"
                    },
                    "values": [{"stringValue": "2026-08-02T11:04:00Z"}],
                },
            ]
        },
    }

    status = get_verification_status(
        URN,
        lambda name, arguments: [entity],
        now=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )

    assert status["stale"] is True
    assert status["stale_reason"] == "receipt has no decision context hash"


class _LiveReceiptHub:
    def __init__(self) -> None:
        self.receipt_number = 0
        self.entity = {
            "urn": URN,
            "name": "orders",
            "properties": {"description": "Current orders."},
            "globalTags": {"tags": [{"tag": {"urn": "urn:li:tag:finance"}}]},
            "ownership": {"owners": [{"owner": "urn:li:corpuser:alice"}]},
            "schemaMetadata": {
                "fields": [{"fieldPath": "order_id", "nativeDataType": "BIGINT"}]
            },
            "relatedDocuments": {
                "start": 0,
                "count": 0,
                "total": 0,
                "documents": [],
            },
            "structuredProperties": {"properties": []},
        }
        self.lineage = {
            False: [
                {
                    "degree": 1,
                    "entity": {
                        "urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,mart,PROD)"
                    },
                }
            ],
            True: [],
        }

    def __call__(self, name: str, arguments: dict) -> object:
        if name == "get_entities":
            return {"entities": [self.entity]}
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            results = self.lineage[bool(arguments["upstream"])]
            return {
                direction: {
                    "total": len(results),
                    "returned": len(results),
                    "hasMore": False,
                    "searchResults": results,
                }
            }
        if name == "save_document":
            self.receipt_number += 1
            document = f"urn:li:document:sidq-receipt-{self.receipt_number}"
            related = self.entity["relatedDocuments"]
            related["documents"].append(
                {
                    "urn": document,
                    "type": "DOCUMENT",
                    "info": {"title": f"Sidq PASS receipt for {URN}"},
                }
            )
            related["count"] += 1
            related["total"] += 1
            return {"urn": document}
        if name == "add_structured_properties":
            properties = self.entity["structuredProperties"]["properties"]
            properties.extend(
                {
                    "structuredProperty": {"urn": urn},
                    "values": [{"stringValue": value} for value in values],
                }
                for urn, values in arguments["property_values"].items()
            )
            return {}
        if name == "add_tags":
            self.entity["globalTags"]["tags"].extend(
                {"tag": {"urn": urn}} for urn in arguments["tag_urns"]
            )
            return {}
        raise AssertionError(name)


@pytest.mark.parametrize(
    "changed", ("owner", "tag", "schema", "downstream_lineage", "upstream_lineage")
)
def test_receipt_context_survives_self_writes_and_detects_semantic_change(
    changed: str,
) -> None:
    hub = _LiveReceiptHub()
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    write_receipt(receipt, hub)
    current = get_verification_status(
        URN, hub, now=datetime(2026, 8, 2, 12, tzinfo=UTC)
    )

    assert current["stale"] is False

    if changed == "owner":
        hub.entity["ownership"]["owners"].append({"owner": "urn:li:corpuser:bob"})
    elif changed == "tag":
        hub.entity["globalTags"]["tags"].append(
            {"tag": {"urn": "urn:li:tag:restricted"}}
        )
    elif changed == "schema":
        hub.entity["schemaMetadata"]["fields"].append(
            {"fieldPath": "email", "nativeDataType": "STRING"}
        )
    else:
        upstream = changed == "upstream_lineage"
        hub.lineage[upstream].append(
            {
                "degree": 1,
                "entity": {"urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,raw,PROD)"},
            }
        )

    stale = get_verification_status(URN, hub, now=datetime(2026, 8, 2, 12, tzinfo=UTC))
    assert stale["stale"] is True
    assert stale["stale_reason"] == "asset decision context changed"


def test_context_hashed_receipt_max_age_boundaries_are_exact() -> None:
    hub = _LiveReceiptHub()
    checked_at = datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    write_receipt(build_receipt(URN, _verdict(), checked_at=checked_at), hub)

    exact = get_verification_status(
        URN, hub, now=checked_at + timedelta(days=7), max_age=timedelta(days=7)
    )
    beyond = get_verification_status(
        URN,
        hub,
        now=checked_at + timedelta(days=7, microseconds=1),
        max_age=timedelta(days=7),
    )
    zero_exact = get_verification_status(URN, hub, now=checked_at, max_age=timedelta(0))
    zero_beyond = get_verification_status(
        URN,
        hub,
        now=checked_at + timedelta(microseconds=1),
        max_age=timedelta(0),
    )

    assert exact["stale"] is False
    assert beyond["stale_reason"] == "receipt exceeded the maximum verification age"
    assert zero_exact["stale"] is False
    assert (
        zero_beyond["stale_reason"] == "receipt exceeded the maximum verification age"
    )


def test_consecutive_receipt_writes_do_not_stale_the_current_receipt() -> None:
    hub = _LiveReceiptHub()
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    write_receipt(receipt, hub)
    write_receipt(receipt, hub)

    current = get_verification_status(
        URN, hub, now=datetime(2026, 8, 2, 12, tzinfo=UTC)
    )
    assert current["stale"] is False


def test_non_sidq_related_documents_are_preserved_once_with_a_stable_hash() -> None:
    documents = [
        {
            "urn": "urn:li:document:runbook",
            "info": {"title": "Operations runbook"},
        },
        {
            "urn": "urn:li:document:contract",
            "info": {"title": "Data contract"},
        },
    ]

    forward = _without_sidq_receipt_documents(documents, evidence_urls=frozenset())
    reversed_order = _without_sidq_receipt_documents(
        list(reversed(documents)), evidence_urls=frozenset()
    )

    assert forward == documents
    assert len(forward) == 2
    assert canonical_json(forward) == canonical_json(reversed_order)


def test_reordered_lineage_does_not_stale_a_receipt() -> None:
    hub = _LiveReceiptHub()
    hub.lineage[False].append(
        {
            "degree": 1,
            "entity": {"urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,second,PROD)"},
        }
    )
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )
    write_receipt(receipt, hub)

    hub.lineage[False].reverse()

    current = get_verification_status(
        URN, hub, now=datetime(2026, 8, 2, 12, tzinfo=UTC)
    )
    assert current["stale"] is False


def test_unprovable_context_is_stale_without_aborting_single_or_batch_reads() -> None:
    hub = _LiveReceiptHub()
    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )
    write_receipt(receipt, hub)

    def truncated(name: str, arguments: dict) -> object:
        response = hub(name, arguments)
        if name == "get_lineage" and not arguments["upstream"]:
            response["downstreams"]["hasMore"] = True
        return response

    single = get_verification_status(
        URN, truncated, now=datetime(2026, 8, 2, 12, tzinfo=UTC)
    )
    batch = get_verification_statuses(
        [URN], truncated, now=datetime(2026, 8, 2, 12, tzinfo=UTC)
    )[URN]

    assert single["stale"] is True
    assert batch["stale"] is True
    assert single["stale_reason"] == "asset decision context could not be proved"
    assert batch["stale_reason"] == "asset decision context could not be proved"


def test_write_refuses_a_truncated_lineage_context() -> None:
    hub = _LiveReceiptHub()

    def truncated(name: str, arguments: dict) -> object:
        response = hub(name, arguments)
        if name == "get_lineage" and not arguments["upstream"]:
            response["downstreams"]["total"] = 2
        return response

    with pytest.raises(RuntimeError, match="incomplete"):
        write_receipt(build_receipt(URN, _verdict()), truncated)


def test_write_accepts_the_official_zero_lineage_shape() -> None:
    hub = _LiveReceiptHub()

    def official_empty(name: str, arguments: dict) -> object:
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "facets": [{"field": "degree", "aggregations": []}],
                }
            }
        return hub(name, arguments)

    written = write_receipt(build_receipt(URN, _verdict()), official_empty)

    assert written["receipt"]["context_hash"].startswith("sha256:")


@pytest.mark.parametrize(
    "section",
    [
        {"total": 0, "returned": 0},
        {"total": 0, "searchResults": []},
        {"total": 0, "hasMore": False},
        {"total": 0, "has_more": False},
        {"total": False},
        {"total": 1, "facets": []},
    ],
)
def test_write_rejects_near_misses_of_the_official_empty_lineage_shape(
    section: dict[str, object],
) -> None:
    hub = _LiveReceiptHub()

    def near_miss(name: str, arguments: dict) -> object:
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {direction: section}
        return hub(name, arguments)

    with pytest.raises(RuntimeError, match="incomplete"):
        write_receipt(build_receipt(URN, _verdict()), near_miss)


@pytest.mark.parametrize(
    "continuation",
    [{}, {"hasMore": "false"}, {"has_more": 0}, {"hasMore": True}],
)
def test_write_requires_explicit_false_lineage_continuation_metadata(
    continuation: dict[str, object],
) -> None:
    hub = _LiveReceiptHub()

    def incomplete(name: str, arguments: dict) -> object:
        response = hub(name, arguments)
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            response[direction].pop("hasMore", None)
            response[direction].update(continuation)
        return response

    with pytest.raises(RuntimeError, match="incomplete"):
        write_receipt(build_receipt(URN, _verdict()), incomplete)


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
        if name == "get_entities":
            return {"entities": [{"urn": URN}]}
        if name == "get_lineage":
            direction = "upstreams" if arguments["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "returned": 0,
                    "hasMore": False,
                    "searchResults": [],
                }
            }
        raise PermissionError("mutation disabled")

    with pytest.raises(PermissionError, match="mutation disabled"):
        write_receipt(receipt, rejected)

    assert calls == ["get_entities", "get_lineage", "get_lineage", "save_document"]
    assert _document_reference({"urn": 42}) == ""
    assert _document_reference("not-a-response") == ""


@pytest.mark.parametrize(
    "saved",
    [
        {"success": True},
        {"success": True, "urn": 42},
        {"success": True, "urn": "https://catalog.example.test/document/1"},
        "urn:li:document:sidq-receipt",
    ],
)
def test_save_document_requires_a_valid_document_urn_before_other_mutations(
    saved: object,
) -> None:
    receipt = build_receipt(URN, _verdict())
    hub = _LiveReceiptHub()
    calls: list[str] = []

    def caller(name: str, arguments: dict) -> object:
        calls.append(name)
        if name == "save_document":
            return saved
        return hub(name, arguments)

    with pytest.raises(RuntimeError, match="valid document URN"):
        write_receipt(receipt, caller)

    assert calls[-1] == "save_document"
    assert "add_structured_properties" not in calls
    assert "add_tags" not in calls


@pytest.mark.parametrize("failed_tool", ["add_structured_properties", "add_tags"])
def test_later_mutation_failure_is_not_reported_as_a_successful_write(
    failed_tool: str,
) -> None:
    receipt = build_receipt(URN, _verdict())
    hub = _LiveReceiptHub()

    def caller(name: str, arguments: dict) -> object:
        if name == failed_tool:
            raise PermissionError(f"{failed_tool} denied")
        return hub(name, arguments)

    outcomes = write_receipts([receipt], caller)

    assert outcomes[0].written is False
    assert outcomes[0].detail == "PermissionError"
    assert "receipts written  0 of 1" in "\n".join(render_writeback(outcomes))
    assert hub.receipt_number == 1  # save_document has no transaction to roll back.


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


def test_mutating_mcp_subprocess_environment_is_closed(monkeypatch) -> None:
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "writer-token")
    monkeypatch.setenv("CLAIMS_SOURCE", "postgresql://reader:secret@warehouse/db")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "ambient-secret")

    environment = _mcp_subprocess_environment("https://catalog.example.test")

    assert environment["DATAHUB_GMS_URL"] == "https://catalog.example.test"
    assert environment["DATAHUB_GMS_TOKEN"] == "writer-token"
    assert environment["DATAHUB_TELEMETRY_ENABLED"] == "false"
    assert environment["TOOLS_IS_MUTATION_ENABLED"] == "true"
    assert environment["LOGURU_LEVEL"] == "WARNING"
    assert "PATH" in environment
    assert "CLAIMS_SOURCE" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "UNRELATED_SECRET" not in environment


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
