from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Self

import anyio
import mcp
import mcp.client.stdio
import pytest

from sidq.agent.writeback import render_writeback, write_receipts
from sidq.models import Evidence, Finding, Verdict
from sidq.policy.engine import PolicyEngine
from sidq.receipt.assertion import (
    DataHubSDKUnavailable,
    assertion_result_type,
    assertion_urn,
    emit_assertions,
    require_sdk,
)
from sidq.receipt.bootstrap import (
    PROPERTY_DEFINITIONS,
    definitions,
    ensure_sidq_properties,
    property_urn,
)
from sidq.receipt.build import build_receipt
from sidq.receipt.read import (
    _semantic_context,
    _sidq_values,
    _without_sidq_receipt_documents,
    get_verification_status,
    get_verification_statuses,
)
from sidq.receipt.write import (
    RECEIPT_READ_TOOLS,
    RECEIPT_TOOLS,
    ReceiptWriteUnconfirmed,
    StdioMCPReceiptToolCaller,
    ToolNotAllowed,
    _document_reference,
    _mcp_subprocess_environment,
    write_receipt,
)
from sidq.receipt.write import _managed_badges as _parsed_managed_badges
from sidq.repair import REPAIR_TOOLS
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
    properties: dict[str, list[str]] = {}
    tags: set[str] = set()

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {
                "entities": [
                    {
                        "urn": URN,
                        "globalTags": {"tags": [{"tag": {"urn": urn}} for urn in tags]},
                        "structuredProperties": {
                            "properties": [
                                {
                                    "structuredProperty": {"urn": urn},
                                    "values": [
                                        {"stringValue": value} for value in values
                                    ],
                                }
                                for urn, values in properties.items()
                            ]
                        },
                    }
                ]
            }
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
        if name == "add_structured_properties":
            properties.update(arguments["property_values"])
        if name == "add_tags":
            tags.update(arguments["tag_urns"])
        return {"success": True, "urn": "urn:li:document:sidq-receipt"}

    written = write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "get_entities",
        "get_lineage",
        "get_lineage",
        "save_document",
        "add_tags",
        "add_structured_properties",
        "get_entities",
    ]
    assert calls[5][1]["property_values"][
        "urn:li:structuredProperty:sidq.evidence_url"
    ] == ["urn:li:document:sidq-receipt"]
    assert calls[5][1]["property_values"][
        "urn:li:structuredProperty:sidq.context_hash"
    ][0].startswith("sha256:")
    assert calls[4][1]["tag_urns"] == ["urn:li:tag:sidq:verified"]
    assert written["receipt"]["evidence_url"] == "urn:li:document:sidq-receipt"


def test_block_receipt_is_written_and_uses_the_blocked_badge() -> None:
    receipt = build_receipt(
        URN, _verdict("BLOCK"), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    calls: list[tuple[str, object]] = []
    properties: dict[str, list[str]] = {}
    tags: set[str] = set()

    def caller(name: str, arguments: object) -> object:
        calls.append((name, arguments))
        if name == "get_entities":
            return {
                "entities": [
                    {
                        "urn": URN,
                        "globalTags": {"tags": [{"tag": {"urn": urn}} for urn in tags]},
                        "structuredProperties": {
                            "properties": [
                                {
                                    "structuredProperty": {"urn": urn},
                                    "values": [
                                        {"stringValue": value} for value in values
                                    ],
                                }
                                for urn, values in properties.items()
                            ]
                        },
                    }
                ]
            }
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
        if name == "add_structured_properties":
            properties.update(arguments["property_values"])
        if name == "add_tags":
            tags.update(arguments["tag_urns"])
        return {"success": True, "urn": "urn:li:document:sidq-blocked"}

    write_receipt(receipt, caller)

    assert [name for name, _ in calls] == [
        "get_entities",
        "get_lineage",
        "get_lineage",
        "save_document",
        "add_tags",
        "add_structured_properties",
        "get_entities",
    ]
    assert calls[4][1]["tag_urns"] == ["urn:li:tag:sidq:blocked"]


def test_managed_badges_reads_the_official_mcp_tags_field() -> None:
    entity = {
        "tags": {
            "tags": [
                {"tag": {"urn": "urn:li:tag:sidq:verified"}},
                {"tag": {"urn": "urn:li:tag:finance"}},
            ]
        }
    }

    assert _parsed_managed_badges(entity) == {"urn:li:tag:sidq:verified"}


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
            updated_urns = set(arguments["property_values"])
            properties[:] = [
                assignment
                for assignment in properties
                if assignment["structuredProperty"]["urn"] not in updated_urns
            ]
            properties.extend(
                {
                    "structuredProperty": {"urn": urn},
                    "values": [{"stringValue": value} for value in values],
                }
                for urn, values in arguments["property_values"].items()
            )
            return {}
        if name == "remove_structured_properties":
            removed_urns = set(arguments["property_urns"])
            properties = self.entity["structuredProperties"]["properties"]
            properties[:] = [
                assignment
                for assignment in properties
                if assignment["structuredProperty"]["urn"] not in removed_urns
            ]
            return {}
        if name == "add_tags":
            tags = self.entity["globalTags"]["tags"]
            existing = {tag["tag"]["urn"] for tag in tags}
            tags.extend(
                {"tag": {"urn": urn}}
                for urn in arguments["tag_urns"]
                if urn not in existing
            )
            return {}
        if name == "remove_tags":
            removed_urns = set(arguments["tag_urns"])
            tags = self.entity["globalTags"]["tags"]
            tags[:] = [tag for tag in tags if tag["tag"]["urn"] not in removed_urns]
            return {}
        raise AssertionError(name)


class _OfficialTagsReceiptHub(_LiveReceiptHub):
    """Model the official MCP response shape observed from live DataHub."""

    def __init__(self) -> None:
        super().__init__()
        del self.entity["globalTags"]

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> object:
        if name in {"add_tags", "remove_tags"}:
            wrapper = self.entity.setdefault("tags", {"tags": []})
            tags = wrapper["tags"]
            affected = set(arguments["tag_urns"])
            if name == "add_tags":
                existing = {tag["tag"]["urn"] for tag in tags}
                tags.extend(
                    {"tag": {"urn": urn}}
                    for urn in arguments["tag_urns"]
                    if urn not in existing
                )
            else:
                tags[:] = [tag for tag in tags if tag["tag"]["urn"] not in affected]
            return {}
        return super().__call__(name, dict(arguments))


def test_official_tags_first_write_confirms_and_remains_current() -> None:
    hub = _OfficialTagsReceiptHub()
    checked_at = datetime(2026, 8, 2, 11, 4, tzinfo=UTC)

    written = write_receipt(build_receipt(URN, _verdict(), checked_at=checked_at), hub)
    status = get_verification_status(URN, hub, now=checked_at + timedelta(minutes=1))

    assert written["confirmed"] is True
    assert _parsed_managed_badges(hub.entity) == {"urn:li:tag:sidq:verified"}
    assert status["stale"] is False


class _DelayedReceiptHub(_LiveReceiptHub):
    def __init__(self, *, hidden_reads: int) -> None:
        super().__init__()
        self.hidden_reads = hidden_reads
        self.confirmation_calls: list[str] = []
        self._properties_written = False

    def __call__(self, name: str, arguments: Mapping[str, Any]) -> object:
        if name == "add_structured_properties":
            result = super().__call__(name, dict(arguments))
            self._properties_written = True
            return result
        if name == "get_entities" and self._properties_written:
            self.confirmation_calls.append(name)
            if self.hidden_reads > 0:
                self.hidden_reads -= 1
                entity = dict(self.entity)
                entity["structuredProperties"] = {"properties": []}
                return {"entities": [entity]}
        return super().__call__(name, dict(arguments))


class _BoundedReceiptHub(_LiveReceiptHub):
    def __init__(self) -> None:
        super().__init__()
        self.bounded_confirmation_calls = 0

    def call_with_timeout(
        self, name: str, arguments: Mapping[str, Any], *, timeout: float
    ) -> object:
        self.bounded_confirmation_calls += 1
        if self.bounded_confirmation_calls == 1:
            raise TimeoutError("first confirmation timed out")
        return super().__call__(name, dict(arguments))


def _managed_badges(hub: _LiveReceiptHub) -> list[str]:
    return sorted(
        tag["tag"]["urn"]
        for tag in hub.entity["globalTags"]["tags"]
        if tag["tag"]["urn"].startswith("urn:li:tag:sidq:")
    )


def test_failed_receipt_body_restores_badges_and_prior_queryable_state() -> None:
    hub = _LiveReceiptHub()
    calls: list[str] = []

    def reject_after_applying(name: str, arguments: Mapping[str, Any]) -> object:
        calls.append(name)
        result = hub(name, dict(arguments))
        if name == "add_structured_properties":
            raise PermissionError("receipt body rejected after an ambiguous mutation")
        return result

    with pytest.raises(PermissionError, match="receipt body rejected"):
        write_receipt(build_receipt(URN, _verdict()), reject_after_applying)

    assert _managed_badges(hub) == []
    assert _sidq_values(hub.entity) == {}
    assert calls[-2:] == ["remove_tags", "remove_structured_properties"]


def test_failed_badge_transition_restores_the_previous_badge_and_body() -> None:
    hub = _LiveReceiptHub()
    write_receipt(build_receipt(URN, _verdict("BLOCK")), hub)
    previous_values = _sidq_values(hub.entity)

    def reject_after_adding(name: str, arguments: Mapping[str, Any]) -> object:
        result = hub(name, dict(arguments))
        if name == "add_tags" and arguments["tag_urns"] == ["urn:li:tag:sidq:verified"]:
            raise PermissionError("new badge rejected after an ambiguous mutation")
        return result

    with pytest.raises(PermissionError, match="new badge rejected"):
        write_receipt(build_receipt(URN, _verdict()), reject_after_adding)

    assert _managed_badges(hub) == ["urn:li:tag:sidq:blocked"]
    assert _sidq_values(hub.entity) == previous_values


@pytest.mark.parametrize(
    ("before", "after", "expected_badge"),
    [
        ("PASS", "BLOCK", "urn:li:tag:sidq:blocked"),
        ("WARN", "BLOCK", "urn:li:tag:sidq:blocked"),
        ("BLOCK", "PASS", "urn:li:tag:sidq:verified"),
        ("BLOCK", "WARN", "urn:li:tag:sidq:verified"),
    ],
)
def test_successful_verdict_transition_leaves_exactly_one_correct_badge(
    before: str, after: str, expected_badge: str
) -> None:
    hub = _LiveReceiptHub()

    write_receipt(build_receipt(URN, _verdict(before)), hub)
    write_receipt(build_receipt(URN, _verdict(after)), hub)

    assert _managed_badges(hub) == [expected_badge]


@pytest.mark.parametrize(
    ("prior_verdict", "ignored_tool"),
    [(None, "add_tags"), ("BLOCK", "remove_tags")],
)
def test_write_does_not_confirm_no_op_managed_badge_mutations(
    prior_verdict: str | None, ignored_tool: str
) -> None:
    hub = _LiveReceiptHub()
    if prior_verdict:
        write_receipt(build_receipt(URN, _verdict(prior_verdict)), hub)
    previous_values = _sidq_values(hub.entity)
    previous_badges = _managed_badges(hub)
    ignored = False

    def ignore_badge_mutation(name: str, arguments: Mapping[str, Any]) -> object:
        nonlocal ignored
        if name == ignored_tool and not ignored:
            ignored = True
            return {}
        return hub(name, dict(arguments))

    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed"):
        write_receipt(
            build_receipt(URN, _verdict()),
            ignore_badge_mutation,
            confirmation_timeout=0.01,
            confirmation_initial_delay=0.01,
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

    assert _sidq_values(hub.entity) == previous_values
    assert _managed_badges(hub) == previous_badges


def test_write_waits_for_exact_receipt_readback_with_bounded_backoff() -> None:
    hub = _DelayedReceiptHub(hidden_reads=2)
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    written = write_receipt(
        build_receipt(URN, _verdict()),
        hub,
        confirmation_timeout=1.0,
        confirmation_initial_delay=0.1,
        confirmation_max_delay=0.2,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    assert written["confirmed"] is True
    assert written["confirmation_attempts"] == 3
    assert sleeps == [0.1, 0.2]
    assert hub.confirmation_calls == ["get_entities"] * 3


def test_write_rejects_conflicting_managed_badges_across_tag_surfaces() -> None:
    class _ConflictingTagSurfaceHub(_LiveReceiptHub):
        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            assert name == "get_entities"
            return {
                "entities": [
                    {
                        **self.entity,
                        "tags": {"tags": [{"tag": {"urn": "urn:li:tag:sidq:blocked"}}]},
                    }
                ]
            }

    hub = _ConflictingTagSurfaceHub()

    with pytest.raises(ReceiptWriteUnconfirmed, match="managed_badge=mismatch"):
        write_receipt(
            build_receipt(URN, _verdict()),
            hub,
            confirmation_timeout=0,
        )


def test_write_rejects_conflicting_tag_urn_aliases() -> None:
    class _ConflictingTagAliasHub(_LiveReceiptHub):
        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            assert name == "get_entities"
            return {
                "entities": [
                    {
                        **self.entity,
                        "globalTags": {
                            "tags": [
                                {
                                    "urn": "urn:li:tag:sidq:verified",
                                    "tag": {"urn": "urn:li:tag:sidq:blocked"},
                                }
                            ]
                        },
                    }
                ]
            }

    with pytest.raises(ReceiptWriteUnconfirmed, match="managed_badge=mismatch"):
        write_receipt(
            build_receipt(URN, _verdict()),
            _ConflictingTagAliasHub(),
            confirmation_timeout=0,
        )


@pytest.mark.parametrize(
    "blocked_surface",
    [
        ["urn:li:tag:sidq:blocked"],
        {"tags": "urn:li:tag:sidq:blocked"},
        "urn:li:tag:sidq:blocked",
        {"urn": "urn:li:tag:sidq:blocked", "tags": []},
    ],
)
def test_write_rejects_conflicting_compact_string_badge_surface(
    blocked_surface: object,
) -> None:
    class _CompactStringConflictHub(_LiveReceiptHub):
        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            assert name == "get_entities"
            return {
                "entities": [
                    {
                        **self.entity,
                        "tags": blocked_surface,
                    }
                ]
            }

    with pytest.raises(ReceiptWriteUnconfirmed, match="managed_badge=mismatch"):
        write_receipt(
            build_receipt(URN, _verdict()),
            _CompactStringConflictHub(),
            confirmation_timeout=0,
        )


def test_default_confirmation_window_tolerates_slow_catalog_indexing() -> None:
    hub = _DelayedReceiptHub(hidden_reads=12)
    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    written = write_receipt(
        build_receipt(URN, _verdict()),
        hub,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )

    assert written["confirmed"] is True
    assert written["confirmation_attempts"] == 13
    assert 8.0 < clock[0] < 30.0


def test_write_acknowledgement_without_visible_receipt_is_not_success() -> None:
    hub = _DelayedReceiptHub(hidden_reads=100)
    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed") as caught:
        write_receipt(
            build_receipt(URN, _verdict()),
            hub,
            confirmation_timeout=0.25,
            confirmation_initial_delay=0.1,
            confirmation_max_delay=0.1,
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

    assert hub.receipt_number == 1
    assert hub.confirmation_calls
    assert "last observed mismatch" in str(caught.value)
    assert "managed_badge=" in str(caught.value)


def test_write_confirmation_transport_call_respects_deadline() -> None:
    hub = _LiveReceiptHub()
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    outcome: Future[dict[str, object]] = Future()

    def blocking(name: str, arguments: Mapping[str, Any]) -> object:
        if name == "get_entities" and hub.receipt_number:
            entered.set()
            release.wait()
            returned.set()
        return hub(name, dict(arguments))

    def write() -> None:
        try:
            outcome.set_result(
                write_receipt(
                    build_receipt(URN, _verdict()),
                    blocking,
                    confirmation_timeout=0.05,
                    confirmation_initial_delay=0.01,
                )
            )
        except BaseException as error:  # noqa: BLE001 - relay the worker outcome
            outcome.set_exception(error)

    worker = threading.Thread(target=write, daemon=True)
    started = time.monotonic()
    worker.start()
    try:
        assert entered.wait(timeout=1.0)
        with pytest.raises(
            ReceiptWriteUnconfirmed, match="write_unconfirmed"
        ) as caught:
            outcome.result(timeout=2.5)
        worker.join(timeout=0.1)
        assert not worker.is_alive()
        assert time.monotonic() - started < 2.5
        assert caught.value.receipt_rollback_errors == (
            "get_entities: ReceiptWriteUnconfirmed",
        )
    finally:
        release.set()
        worker.join(timeout=1.0)
    assert returned.wait(timeout=1.0)


def test_a_timed_out_confirmation_does_not_poison_the_shared_bounded_caller() -> None:
    hub = _BoundedReceiptHub()
    receipt = build_receipt(URN, _verdict())

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed"):
        write_receipt(receipt, hub, confirmation_timeout=0.01)

    written = write_receipt(receipt, hub, confirmation_timeout=0.01)

    assert written["confirmed"] is True
    assert hub.bounded_confirmation_calls == 3


def test_solo_write_removes_stale_swarm_assignments_before_exact_confirmation() -> None:
    properties: dict[str, list[str]] = {}
    tags: set[str] = set()
    calls: list[tuple[str, dict[str, Any]]] = []
    document = 0

    def caller(name: str, arguments: Mapping[str, Any]) -> object:
        nonlocal document
        copied = dict(arguments)
        calls.append((name, copied))
        if name == "get_entities":
            return {
                "entities": [
                    {
                        "urn": URN,
                        "globalTags": {"tags": [{"tag": {"urn": urn}} for urn in tags]},
                        "structuredProperties": {
                            "properties": [
                                {
                                    "structuredProperty": {"urn": urn},
                                    "values": [
                                        {"stringValue": value} for value in values
                                    ],
                                }
                                for urn, values in properties.items()
                            ]
                        },
                    }
                ]
            }
        if name == "get_lineage":
            direction = "upstreams" if copied["upstream"] else "downstreams"
            return {
                direction: {
                    "total": 0,
                    "returned": 0,
                    "hasMore": False,
                    "searchResults": [],
                }
            }
        if name == "save_document":
            document += 1
            return {"urn": f"urn:li:document:sidq-receipt-{document}"}
        if name == "add_structured_properties":
            properties.update(copied["property_values"])
            return {}
        if name == "remove_structured_properties":
            for property_urn in copied["property_urns"]:
                properties.pop(property_urn, None)
            return {}
        if name == "add_tags":
            tags.update(copied["tag_urns"])
            return {}
        if name == "remove_tags":
            tags.difference_update(copied["tag_urns"])
            return {}
        raise AssertionError(name)

    write_receipt(
        build_receipt(URN, _verdict(), swarm_run="swarm-1", worker_id="worker-1"),
        caller,
    )
    written = write_receipt(build_receipt(URN, _verdict()), caller)

    removals = [
        arguments for name, arguments in calls if name == "remove_structured_properties"
    ]
    assert removals == [
        {
            "property_urns": [
                "urn:li:structuredProperty:sidq.swarm_run",
                "urn:li:structuredProperty:sidq.worker_id",
            ],
            "entity_urns": [URN],
        }
    ]
    assert written["confirmed"] is True
    assert "urn:li:structuredProperty:sidq.swarm_run" not in properties
    assert "urn:li:structuredProperty:sidq.worker_id" not in properties


def test_failed_solo_write_restores_existing_swarm_receipt_provenance() -> None:
    hub = _LiveReceiptHub()
    swarm = build_receipt(
        URN,
        _verdict("BLOCK"),
        checked_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
        swarm_run="swarm-1",
        worker_id="worker-1",
    )
    write_receipt(swarm, hub)
    previous_values = _sidq_values(hub.entity)

    def reject_solo_body(name: str, arguments: Mapping[str, Any]) -> object:
        result = hub(name, dict(arguments))
        if name == "add_structured_properties":
            raise PermissionError("solo receipt body rejected")
        return result

    with pytest.raises(PermissionError, match="solo receipt body rejected"):
        write_receipt(
            build_receipt(
                URN,
                _verdict(),
                checked_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
            ),
            reject_solo_body,
        )

    assert _sidq_values(hub.entity) == previous_values
    assert _managed_badges(hub) == ["urn:li:tag:sidq:blocked"]


def test_unconfirmed_write_restores_complete_prior_block_swarm_state() -> None:
    hub = _LiveReceiptHub()
    write_receipt(
        build_receipt(
            URN,
            _verdict("BLOCK"),
            checked_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            swarm_run="swarm-1",
            worker_id="worker-1",
        ),
        hub,
    )
    previous_values = _sidq_values(hub.entity)
    previous_badges = _managed_badges(hub)
    ignored_removal = False

    def leave_stale_block_badge(name: str, arguments: Mapping[str, Any]) -> object:
        nonlocal ignored_removal
        if name == "remove_tags" and not ignored_removal:
            ignored_removal = True
            return {}
        return hub(name, dict(arguments))

    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed"):
        write_receipt(
            build_receipt(
                URN,
                _verdict(),
                checked_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
            ),
            leave_stale_block_badge,
            confirmation_timeout=0.01,
            confirmation_initial_delay=0.01,
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

    assert _sidq_values(hub.entity) == previous_values
    assert _managed_badges(hub) == previous_badges


@pytest.mark.parametrize("persistence", ["partial", "mismatched"])
def test_write_does_not_confirm_inexact_structured_properties(
    persistence: str,
) -> None:
    hub = _LiveReceiptHub()

    def inexact(name: str, arguments: Mapping[str, Any]) -> object:
        if name == "add_structured_properties":
            property_values = dict(arguments["property_values"])
            if persistence == "partial":
                property_values.pop("urn:li:structuredProperty:sidq.context_hash")
            else:
                property_values["urn:li:structuredProperty:sidq.verdict"] = ["BLOCK"]
            arguments = {**arguments, "property_values": property_values}
        return hub(name, dict(arguments))

    clock = [0.0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed"):
        write_receipt(
            build_receipt(URN, _verdict()),
            inexact,
            confirmation_timeout=0.01,
            confirmation_initial_delay=0.01,
            monotonic=lambda: clock[0],
            sleep=sleep,
        )


def test_later_same_urn_writer_is_not_erased_by_earlier_writer_rollback() -> None:
    class _RaceHub(_LiveReceiptHub):
        def __init__(self) -> None:
            super().__init__()
            self.a_confirmation_entered = threading.Event()
            self.b_finished = threading.Event()

        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            if threading.current_thread().name == "writer-a":
                self.a_confirmation_entered.set()
                if self.b_finished.wait(timeout=0.2):
                    return super().__call__(name, dict(arguments))
                entity = dict(self.entity)
                entity["structuredProperties"] = {"properties": []}
                entity["globalTags"] = {"tags": []}
                return {"entities": [entity]}
            return super().__call__(name, dict(arguments))

    hub = _RaceHub()
    a_outcome: Future[object] = Future()
    b_outcome: Future[object] = Future()
    b_state: dict[str, object] = {}

    def writer_a() -> None:
        try:
            a_outcome.set_result(
                write_receipt(
                    build_receipt(
                        URN,
                        _verdict(),
                        checked_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
                    ),
                    hub,
                    confirmation_timeout=0,
                )
            )
        except BaseException as error:  # noqa: BLE001 - relay thread outcome
            a_outcome.set_exception(error)

    def writer_b() -> None:
        try:
            b_outcome.set_result(
                write_receipt(
                    build_receipt(
                        URN,
                        _verdict("BLOCK"),
                        checked_at=datetime(2026, 8, 2, 11, tzinfo=UTC),
                    ),
                    hub,
                )
            )
            b_state["properties"] = _sidq_values(hub.entity)
            b_state["badges"] = _managed_badges(hub)
        except BaseException as error:  # noqa: BLE001 - relay thread outcome
            b_outcome.set_exception(error)
        finally:
            hub.b_finished.set()

    a_thread = threading.Thread(target=writer_a, name="writer-a", daemon=True)
    b_thread = threading.Thread(target=writer_b, name="writer-b", daemon=True)
    a_thread.start()
    assert hub.a_confirmation_entered.wait(timeout=1.0)
    b_thread.start()
    a_thread.join(timeout=2.0)
    b_thread.join(timeout=2.0)

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed"):
        a_outcome.result(timeout=0.1)
    assert b_outcome.result(timeout=0.1)["confirmed"] is True
    assert _sidq_values(hub.entity) == b_state["properties"]
    assert _managed_badges(hub) == b_state["badges"] == ["urn:li:tag:sidq:blocked"]


def test_compensation_refuses_to_erase_external_writer_managed_state() -> None:
    class _ExternalWriterHub(_LiveReceiptHub):
        def __init__(self) -> None:
            super().__init__()
            self.external_values: dict[str, list[str]] = {}

        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            if not self.external_values:
                current = _sidq_values(self.entity)
                current["verdict"] = ["BLOCK"]
                current["checked_at"] = ["2026-08-02T11:00:00Z"]
                current["evidence_url"] = ["urn:li:document:external-writer"]
                self.external_values = current
                super().__call__(
                    "add_structured_properties",
                    {
                        "property_values": {
                            f"urn:li:structuredProperty:sidq.{key}": values
                            for key, values in current.items()
                        },
                        "entity_urns": [URN],
                    },
                )
                super().__call__(
                    "remove_tags",
                    {
                        "tag_urns": [
                            "urn:li:tag:sidq:verified",
                            "urn:li:tag:sidq:blocked",
                        ],
                        "entity_urns": [URN],
                    },
                )
                super().__call__(
                    "add_tags",
                    {
                        "tag_urns": ["urn:li:tag:sidq:blocked"],
                        "entity_urns": [URN],
                    },
                )
            return super().__call__(name, dict(arguments))

    hub = _ExternalWriterHub()

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed") as caught:
        write_receipt(
            build_receipt(
                URN,
                _verdict(),
                checked_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            ),
            hub,
            confirmation_timeout=0,
        )

    assert caught.value.receipt_rollback_errors == (
        "state_conflict: concurrent managed receipt detected",
    )
    assert _sidq_values(hub.entity) == hub.external_values
    assert _managed_badges(hub) == ["urn:li:tag:sidq:blocked"]


def test_compensation_treats_external_property_removal_as_a_conflict() -> None:
    class _ExternalRemovalHub(_LiveReceiptHub):
        def __init__(self) -> None:
            super().__init__()
            self.armed = False
            self.removed = False

        def call_with_timeout(
            self, name: str, arguments: Mapping[str, Any], *, timeout: float
        ) -> object:
            if self.armed and not self.removed:
                self.removed = True
                super().__call__(
                    "remove_structured_properties",
                    {
                        "property_urns": ["urn:li:structuredProperty:sidq.checked_at"],
                        "entity_urns": [URN],
                    },
                )
            return super().__call__(name, dict(arguments))

    hub = _ExternalRemovalHub()
    write_receipt(
        build_receipt(
            URN,
            _verdict("BLOCK"),
            checked_at=datetime(2026, 8, 2, 9, tzinfo=UTC),
        ),
        hub,
    )
    hub.armed = True

    with pytest.raises(ReceiptWriteUnconfirmed, match="write_unconfirmed") as caught:
        write_receipt(
            build_receipt(
                URN,
                _verdict(),
                checked_at=datetime(2026, 8, 2, 10, tzinfo=UTC),
            ),
            hub,
            confirmation_timeout=0,
        )

    assert caught.value.receipt_rollback_errors == (
        "state_conflict: concurrent managed receipt detected",
    )
    assert "checked_at" not in _sidq_values(hub.entity)


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


def test_first_managed_badge_on_an_untagged_asset_does_not_change_context() -> None:
    before = {"urn": URN, "name": "orders"}
    after = {
        **before,
        "tags": {
            "tags": [
                {"tag": {"urn": "urn:li:tag:sidq:verified"}},
            ]
        },
    }

    assert _semantic_context(after) == _semantic_context(before)


@pytest.mark.parametrize("tag_field", ["tags", "globalTags", "global_tags"])
def test_managed_badge_filter_preserves_business_tag_context(tag_field: str) -> None:
    finance = {"tag": {"urn": "urn:li:tag:finance"}}
    restricted = {"tag": {"urn": "urn:li:tag:restricted"}}
    sidq = {"tag": {"urn": "urn:li:tag:sidq:verified"}}
    before = {"urn": URN, tag_field: {"tags": [finance]}}
    after_self_write = {"urn": URN, tag_field: {"tags": [finance, sidq]}}
    after_business_change = {
        "urn": URN,
        tag_field: {"tags": [finance, restricted, sidq]},
    }

    assert _semantic_context(after_self_write) == _semantic_context(before)
    assert _semantic_context(after_business_change) != _semantic_context(before)


def test_compact_string_badges_are_parsed_without_dropping_business_tags() -> None:
    before = {"tags": ["urn:li:tag:finance"]}
    after_self_write = {"tags": ["urn:li:tag:finance", "urn:li:tag:sidq:verified"]}
    after_business_change = {
        "tags": [
            "urn:li:tag:finance",
            "urn:li:tag:restricted",
            "urn:li:tag:sidq:verified",
        ]
    }

    assert _parsed_managed_badges(after_self_write) == {"urn:li:tag:sidq:verified"}
    assert _semantic_context(after_self_write) == _semantic_context(before)
    assert _semantic_context(after_business_change) != _semantic_context(before)
    for managed_only in (
        {"tags": "urn:li:tag:sidq:verified"},
        {"tags": {"tags": "urn:li:tag:sidq:verified"}},
    ):
        assert _parsed_managed_badges(managed_only) == {"urn:li:tag:sidq:verified"}
        assert _semantic_context(managed_only) == {}
    hybrid = {"tags": {"urn": "urn:li:tag:sidq:verified", "tags": []}}
    assert _semantic_context(hybrid) != {}
    nested_before = {"tags": [["urn:li:tag:finance"]]}
    nested_after = {"tags": [["urn:li:tag:finance", "urn:li:tag:sidq:verified"]]}
    assert _parsed_managed_badges(nested_after) == {"urn:li:tag:sidq:verified"}
    assert _semantic_context(nested_after) == _semantic_context(nested_before)
    metadata_before = {
        "tags": [
            {
                "tag": {"urn": "urn:li:tag:finance"},
                "properties": {"references": []},
            }
        ]
    }
    metadata_after = {
        "tags": [
            {
                "tag": {"urn": "urn:li:tag:finance"},
                "properties": {"references": ["urn:li:tag:sidq:verified"]},
            }
        ]
    }
    assert _semantic_context(metadata_after) != _semantic_context(metadata_before)
    nested_tag_key_before = {
        "tags": [
            {
                "tag": {"urn": "urn:li:tag:finance"},
                "properties": {"tags": []},
            }
        ]
    }
    nested_tag_key_after = {
        "tags": [
            {
                "tag": {"urn": "urn:li:tag:finance"},
                "properties": {"tags": ["urn:li:tag:sidq:verified"]},
            }
        ]
    }
    assert _semantic_context(nested_tag_key_after) != _semantic_context(
        nested_tag_key_before
    )
    arbitrary_before = {"customProperties": {}}
    arbitrary_after = {"customProperties": {"tags": "urn:li:tag:sidq:verified"}}
    assert _semantic_context(arbitrary_after) != _semantic_context(arbitrary_before)


def test_badge_like_description_cannot_hide_a_business_tag_change() -> None:
    hub = _LiveReceiptHub()
    tag = hub.entity["globalTags"]["tags"][0]
    tag["description"] = "urn:li:tag:sidq:verified"
    checked_at = datetime(2026, 8, 2, 11, 4, tzinfo=UTC)

    write_receipt(build_receipt(URN, _verdict(), checked_at=checked_at), hub)
    tag["tag"]["urn"] = "urn:li:tag:restricted"
    status = get_verification_status(URN, hub, now=checked_at + timedelta(minutes=1))

    assert status["stale"] is True
    assert status["stale_reason"] == "asset decision context changed"


def test_conflicting_tag_urn_aliases_cannot_hide_a_business_tag_change() -> None:
    hub = _LiveReceiptHub()
    tag = hub.entity["globalTags"]["tags"][0]
    checked_at = datetime(2026, 8, 2, 11, 4, tzinfo=UTC)

    write_receipt(build_receipt(URN, _verdict(), checked_at=checked_at), hub)
    tag["urn"] = "urn:li:tag:sidq:verified"
    tag["tag"]["urn"] = "urn:li:tag:restricted"
    status = get_verification_status(URN, hub, now=checked_at + timedelta(minutes=1))

    assert status["stale"] is True
    assert status["stale_reason"] == "asset decision context changed"


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

    class _AssertionType:
        CUSTOM = "CUSTOM"

    class _AssertionSourceType:
        EXTERNAL = "EXTERNAL"

    class _AssertionRunStatus:
        COMPLETE = "COMPLETE"

    class _AssertionResultType:
        SUCCESS = "SUCCESS"
        FAILURE = "FAILURE"

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
    schema.AssertionInfoClass = _BootstrapValue
    schema.CustomAssertionInfoClass = _BootstrapValue
    schema.AssertionSourceClass = _BootstrapValue
    schema.AssertionSourceTypeClass = _AssertionSourceType
    schema.AssertionTypeClass = _AssertionType
    schema.AssertionRunEventClass = _BootstrapValue
    schema.AssertionRunStatusClass = _AssertionRunStatus
    schema.AssertionResultClass = _BootstrapValue
    schema.AssertionResultTypeClass = _AssertionResultType
    schema.StatusClass = _StatusAspect
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


class _StatusAspect:
    """Distinct from the catch-all fake value, so get_aspect can tell them apart."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _AssertionGraph:
    class RelationshipDirection:
        INCOMING = "INCOMING"

    def __init__(self) -> None:
        self.infos: dict[str, object] = {}
        self.run_events: dict[tuple[str, str], object] = {}
        self.asserts: dict[str, list[str]] = {}
        self.soft_deleted: set[str] = set()
        self.closed = False

    def get_aspect(self, urn: str, aspect_type: object) -> object | None:
        if urn in self.soft_deleted:
            return (
                SimpleNamespace(removed=True) if aspect_type is _StatusAspect else None
            )
        return None if aspect_type is _StatusAspect else self.infos.get(urn)

    def get_related_entities(
        self, urn: str, relationships: list[str], direction: str
    ) -> list[SimpleNamespace]:
        return [SimpleNamespace(urn=item) for item in sorted(self.asserts.get(urn, []))]

    def emit_mcp(self, mcp: object) -> None:
        aspect = mcp.aspect
        if hasattr(aspect, "customAssertion"):
            self.infos[mcp.entityUrn] = aspect
            self.asserts.setdefault(aspect.customAssertion.entity, [])
            if mcp.entityUrn not in self.asserts[aspect.customAssertion.entity]:
                self.asserts[aspect.customAssertion.entity].append(mcp.entityUrn)
        else:
            self.run_events[(mcp.entityUrn, aspect.messageId)] = aspect

    def close(self) -> None:
        self.closed = True


def test_native_assertion_urn_is_stable_for_one_dataset_rule() -> None:
    assert assertion_urn(URN, "schema.required") == assertion_urn(
        URN, "schema.required"
    )
    assert assertion_urn(URN, "schema.required") != assertion_urn(URN, "pii.leak")
    assert assertion_urn(URN, "schema.required") != assertion_urn(
        URN + ".other", "schema.required"
    )


@pytest.mark.parametrize(
    ("verdict", "expected"),
    (("PASS", "SUCCESS"), ("BLOCK", "FAILURE")),
)
def test_native_assertion_result_maps_sidq_verdicts(
    verdict: str, expected: str
) -> None:
    assert assertion_result_type(verdict) == expected


def test_native_assertion_emission_is_idempotent_and_carries_evidence(
    monkeypatch,
) -> None:
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    receipt = build_receipt(
        URN, _verdict("WARN"), checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    first = emit_assertions([receipt], graph)
    second = emit_assertions([receipt], graph)

    urn = assertion_urn(URN, "schema.required")
    assert first["created"] == (urn,)
    assert second["existing"] == (urn,)
    assert len(graph.infos) == 1
    # A stable message id lets DataHub update the same time-series event on retry.
    assert len(graph.run_events) == 1
    info = graph.infos[urn]
    event = next(iter(graph.run_events.values()))
    assert info.type == "CUSTOM"
    assert info.source.type == "EXTERNAL"
    # DataHub lists the description as the assertion's name, so it stays short
    # and the reasoning lives in the logic field beside it.
    assert info.description == "Sidq policy rule schema.required"
    assert info.customAssertion.logic.startswith("Sidq rule schema.required:")
    assert event.status == "COMPLETE"
    assert event.result.type == "FAILURE"
    assert event.result.nativeResults == {
        "sidq.verdict": "WARN",
        "sidq.rule_id": "schema.required",
        "sidq.severity": "block",
        "sidq.policy_hash": "sha256:policy",
        "sidq.commit_sha": "a" * 40,
        "sidq.checked_at": "2026-08-02T11:04:00Z",
        "sidq.evidence_summary": "A required field is missing.",
    }


def test_a_rule_is_reported_by_its_own_severity_not_the_receipt_verdict(
    monkeypatch,
) -> None:
    """A BLOCK receipt can carry a rule that did not block.

    Publishing the receipt-wide verdict against every rule would paint an
    `info` finding red in the catalog's quality surface and state, of a rule
    that passed, that it failed.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    blocking = Finding("critical_downstream", "block", "Nine owners depend.", ())
    context = Finding("pii_marker", "info", "The field is marked PII.", ())
    verdict = Verdict(
        "BLOCK", "MISSING_FIELD", (blocking, context), (), "a" * 40, "sha256:policy"
    )
    receipt = build_receipt(URN, verdict, checked_at=datetime(2026, 8, 2, tzinfo=UTC))

    emit_assertions([receipt], graph)

    results = {
        event.result.nativeResults["sidq.rule_id"]: event.result.type
        for event in graph.run_events.values()
    }
    assert results == {"critical_downstream": "FAILURE", "pii_marker": "SUCCESS"}


def test_re_emission_updates_the_definition_rather_than_leaving_it_stale(
    monkeypatch,
) -> None:
    """The definition carries this run's reasoning, so it is rewritten each run.

    Writing it only on first sight would leave the catalog showing the first
    run's evidence forever while the run events beneath it said otherwise.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    first = Finding("schema.required", "block", "One field is missing.", ())
    later = Finding("schema.required", "block", "Three fields are missing.", ())
    stamp = datetime(2026, 8, 2, tzinfo=UTC)

    for finding in (first, later):
        emit_assertions(
            [
                build_receipt(
                    URN,
                    Verdict("BLOCK", None, (finding,), (), "a" * 40, "sha256:policy"),
                    checked_at=stamp,
                )
            ],
            graph,
        )

    logic = graph.infos[assertion_urn(URN, "schema.required")].customAssertion.logic
    assert logic == "Sidq rule schema.required: Three fields are missing."


def test_native_assertion_uses_configured_gms_url_and_token(monkeypatch) -> None:
    graph = _AssertionGraph()
    configs = _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    monkeypatch.setenv("DATAHUB_GMS_URL", "https://catalog.env.test")
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "writer-token")

    emit_assertions(
        [build_receipt(URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC))]
    )

    assert configs[0].server == "https://catalog.env.test"
    assert configs[0].token == "writer-token"
    assert graph.closed


def test_a_missing_datahub_sdk_names_the_boundary_it_hit() -> None:
    """The project venv has no DataHub SDK, and that is a documented choice.

    acryl-datahub pins pydantic below 2.12 while Sidq's mcp>=2 client needs
    2.12 or newer, so the SDK cannot become an extra without breaking every
    other command. An operator who reaches this path must be told that, not
    handed a bare ModuleNotFoundError to decode. No mock here on purpose: the
    condition under test is this environment.
    """
    import importlib.util

    if importlib.util.find_spec("datahub") is not None:
        pytest.skip("this pins the message seen when the SDK is absent")

    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    # Both entry points must say the same thing: the precondition a caller
    # checks first, and the emission itself if a caller skipped it.
    with pytest.raises(DataHubSDKUnavailable, match="pydantic"):
        require_sdk()
    with pytest.raises(DataHubSDKUnavailable, match="pydantic"):
        emit_assertions([receipt])


def test_a_warning_says_so_in_the_one_place_the_tab_renders(monkeypatch) -> None:
    """DataHub's chip counts a WARN with the failures and cannot be told not to.

    The row can still say what it is, and retirement must still recover the
    rule id from that decorated name.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    warned = Finding("wide_blast_radius", "warn", "Sixteen consumers.", ())
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("WARN", None, (warned,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )

    urn = assertion_urn(URN, "wide_blast_radius")
    assert (
        graph.infos[urn].description == "Sidq policy rule wide_blast_radius (warning)"
    )
    # It still reports FAILURE, because the condition did not pass.
    assert graph.run_events[next(iter(graph.run_events))].result.type == "FAILURE"

    # And the decorated name must not break the retirement round trip.
    result = emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("PASS", None, (), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )
    assert result["retired"] == (urn,)
    closing = [
        event
        for event in graph.run_events.values()
        if event.assertionUrn == urn and event.result.type == "SUCCESS"
    ]
    assert closing[0].result.nativeResults["sidq.rule_id"] == "wide_blast_radius"


def test_a_rule_that_stops_firing_is_retired_not_left_claiming_failure(
    monkeypatch,
) -> None:
    """A fixed problem must stop showing red beside the run that fixed it.

    The catalog keeps every assertion ever written, so a rule Sidq no longer
    reports would otherwise state a failure Sidq no longer holds.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())
    emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("BLOCK", None, (fired,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )
    stale = assertion_urn(URN, "critical_downstream")
    assert graph.run_events[next(iter(graph.run_events))].result.type == "FAILURE"

    # The next run is clean: the rule is simply absent from the evidence.
    result = emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("PASS", None, (), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )

    assert result["retired"] == (stale,)
    closing = [
        event
        for event in graph.run_events.values()
        if event.assertionUrn == stale and event.result.type == "SUCCESS"
    ]
    assert len(closing) == 1
    assert closing[0].result.nativeResults["sidq.rule_id"] == "critical_downstream"
    assert closing[0].result.nativeResults["sidq.severity"] == "retired"


def test_a_firing_rule_does_not_resurrect_an_assertion_an_operator_removed(
    monkeypatch,
) -> None:
    """Deletion is a decision, and a later run must not silently reverse it.

    Retirement already respected soft deletes; the emit path did not, so any
    rule that fired a second time wrote its row straight back into the tab.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())
    verdict = Verdict("BLOCK", None, (fired,), (), "a" * 40, "sha256:policy")
    emit_assertions([build_receipt(URN, verdict, checked_at=stamp)], graph)
    urn = assertion_urn(URN, "critical_downstream")
    graph.soft_deleted.add(urn)
    before = len(graph.run_events)

    result = emit_assertions([build_receipt(URN, verdict, checked_at=stamp)], graph)

    assert result["skipped"] == (urn,)
    assert result["created"] == () and result["existing"] == ()
    assert len(graph.run_events) == before


def test_a_retired_rule_is_not_retired_again_on_every_later_run(monkeypatch) -> None:
    """Closing it once is the point; closing it daily forever is noise."""

    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())
    clean = Verdict("PASS", None, (), (), "a" * 40, "sha256:policy")
    emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("BLOCK", None, (fired,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )

    first = emit_assertions([build_receipt(URN, clean, checked_at=stamp)], graph)
    second = emit_assertions([build_receipt(URN, clean, checked_at=stamp)], graph)

    assert first["retired"] == (assertion_urn(URN, "critical_downstream"),)
    assert second["retired"] == ()


def test_the_whole_verdict_row_is_never_retired(monkeypatch) -> None:
    """The verdict always exists; it is not a rule that can stop firing.

    Retiring it would make a dataset that alternates clean and dirty thrash the
    same assertion between retired and recreated forever.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("PASS", None, (), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())

    result = emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("BLOCK", None, (fired,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )

    assert result["retired"] == ()


def test_two_receipts_for_one_dataset_do_not_retire_each_other(monkeypatch) -> None:
    """`emit_assertions` is a public API; nothing stops a caller doing this."""

    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    one = Finding("critical_downstream", "block", "Nine owners depend.", ())
    two = Finding("wide_blast_radius", "warn", "Sixteen consumers.", ())

    result = emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("BLOCK", None, (one,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            ),
            build_receipt(
                URN,
                Verdict("WARN", None, (two,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            ),
        ],
        graph,
    )

    assert result["retired"] == ()
    assert len(result["created"]) == 2


def test_retirement_does_not_resurrect_an_assertion_an_operator_removed(
    monkeypatch,
) -> None:
    """DataHub keeps the Asserts relationship after a soft delete.

    Measured on 2026-08-09: without this filter, retiring emitted a fresh run
    event against a soft-deleted assertion and pulled it back into the Quality
    tab the operator had just cleared.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    stamp = datetime(2026, 8, 2, tzinfo=UTC)
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())
    emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("BLOCK", None, (fired,), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )
    graph.soft_deleted.add(assertion_urn(URN, "critical_downstream"))

    result = emit_assertions(
        [
            build_receipt(
                URN,
                Verdict("PASS", None, (), (), "a" * 40, "sha256:policy"),
                checked_at=stamp,
            )
        ],
        graph,
    )

    assert result["retired"] == ()


def test_retirement_leaves_assertions_sidq_did_not_write_alone(monkeypatch) -> None:
    """A dbt or Snowflake check on the same dataset is somebody else's claim."""

    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    foreign = "urn:li:assertion:dbt-not-null-order-id"
    graph.asserts[URN] = [foreign]
    graph.infos[foreign] = SimpleNamespace(
        description="dbt not_null order_id",
        customAssertion=SimpleNamespace(type="dbt.test", entity=URN),
    )

    result = emit_assertions(
        [build_receipt(URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC))],
        graph,
    )

    assert result["retired"] == ()
    assert not [
        event for event in graph.run_events.values() if event.assertionUrn == foreign
    ]


def test_nothing_to_mirror_never_opens_a_catalog_connection(monkeypatch) -> None:
    """When every receipt write was rejected there is no verdict to report."""

    configs = _install_fake_datahub_sdk(monkeypatch, _AssertionGraph())  # type: ignore[arg-type]

    assert emit_assertions([]) == {
        "created": (),
        "existing": (),
        "runs": (),
        "retired": (),
        "skipped": (),
    }
    assert configs == []


def test_a_verdict_datahub_cannot_represent_is_refused_not_guessed() -> None:
    """A fourth verdict must stop the mirror, not silently become a pass."""

    with pytest.raises(ValueError, match="unsupported Sidq verdict: SKIPPED"):
        assertion_result_type("SKIPPED")


def test_a_clean_receipt_still_reports_one_honest_assertion(monkeypatch) -> None:
    """A PASS with no fired rule has no per-rule evidence to group.

    Emitting nothing would leave the Validation surface silent about an asset
    Sidq did examine, so the run is reported under one whole-verdict assertion
    that says plainly that no individual rule evidence was recorded.
    """
    graph = _AssertionGraph()
    _install_fake_datahub_sdk(monkeypatch, graph)  # type: ignore[arg-type]
    clean = Verdict("PASS", None, (), (), "a" * 40, "sha256:policy")
    receipt = build_receipt(
        URN, clean, checked_at=datetime(2026, 8, 2, 11, 4, tzinfo=UTC)
    )

    result = emit_assertions([receipt], graph)

    urn = assertion_urn(URN, "sidq.verdict")
    assert result["created"] == (urn,)
    event = graph.run_events[next(iter(graph.run_events))]
    assert event.result.type == "SUCCESS"
    assert event.result.nativeResults["sidq.rule_id"] == "sidq.verdict"
    assert (
        event.result.nativeResults["sidq.evidence_summary"]
        == "No individual rule evidence was recorded."
    )


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
    assert get_verification_status(URN, hub)["verdict"] is None


def test_rollback_failure_is_reported_without_transport_error_secrets() -> None:
    receipt = build_receipt(URN, _verdict())
    hub = _LiveReceiptHub()

    def failed_write_and_rollback(name: str, arguments: dict) -> object:
        result = hub(name, arguments)
        if name == "add_structured_properties":
            raise PermissionError("write failed with token=write-secret")
        if name == "remove_structured_properties":
            raise RuntimeError("rollback failed with token=rollback-secret")
        return result

    outcomes = write_receipts([receipt], failed_write_and_rollback)

    assert outcomes[0].written is False
    assert outcomes[0].detail == (
        "PermissionError; rollback_incomplete: "
        "remove_structured_properties: RuntimeError"
    )
    assert "secret" not in "\n".join(render_writeback(outcomes))


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
    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
    caller._requests = _ImmediateQueue()  # type: ignore[assignment]
    monkeypatch.setattr("sidq.receipt.write.threading.Thread", _ImmediateThread)
    monkeypatch.setattr(caller, "_run", lambda: caller._startup.set_result(None))

    assert caller("add_tags", {}) == {"ok": True}
    thread = caller._thread
    assert thread is not None
    caller.close()
    assert thread.joined is True
    assert caller._thread is None


def test_mutating_mcp_subprocess_environment_is_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATAHUB_GMS_TOKEN", "writer-token")
    monkeypatch.setenv("CLAIMS_SOURCE", "postgresql://reader:secret@warehouse/db")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "ambient-secret")

    private_home = tmp_path / "receipt-mcp-home"
    environment = _mcp_subprocess_environment(
        "https://catalog.example.test", home=str(private_home)
    )

    assert environment["DATAHUB_GMS_URL"] == "https://catalog.example.test"
    assert environment["DATAHUB_GMS_TOKEN"] == "writer-token"
    assert environment["DATAHUB_TELEMETRY_ENABLED"] == "false"
    assert environment["TOOLS_IS_MUTATION_ENABLED"] == "true"
    assert environment["LOGURU_LEVEL"] == "WARNING"
    assert environment["HOME"] == str(private_home)
    assert environment["HOME"] != "/tmp"
    assert "PATH" in environment
    assert "CLAIMS_SOURCE" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "UNRELATED_SECRET" not in environment


def test_receipt_stdio_uses_and_removes_a_private_temporary_home(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
    captured_homes: list[Path] = []

    class _Session:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def next_request(function):
        return None

    def stdio_client(parameters):
        home = Path(parameters["env"]["HOME"])
        assert home.is_dir()
        assert home.name.startswith("sidq-receipt-mcp-")
        captured_homes.append(home)
        return _AsyncContext((object(), object()))

    monkeypatch.setattr(anyio.to_thread, "run_sync", next_request)
    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio_client)
    monkeypatch.setattr(
        mcp.client.stdio, "StdioServerParameters", lambda **kwargs: kwargs
    )

    asyncio.run(caller._serve())

    assert len(captured_homes) == 1
    assert not captured_homes[0].exists()


def test_receipt_stdio_startup_timeout_is_relayed(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)

    class _AnyIO:
        @staticmethod
        def run(function) -> None:
            raise TimeoutError("MCP startup timed out")

    monkeypatch.setitem(sys.modules, "anyio", _AnyIO)
    caller._run()

    with pytest.raises(TimeoutError, match="startup timed out"):
        caller._startup.result()


def test_receipt_stdio_bounded_request_times_out_during_startup(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
    entered = threading.Event()
    release = threading.Event()

    def blocked_startup() -> None:
        entered.set()
        release.wait()
        caller._startup.set_result(None)

    monkeypatch.setattr(caller, "_run", blocked_startup)

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            caller.call_with_timeout("get_entities", {}, timeout=0.05)
        assert entered.is_set()
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        if caller._thread is not None:
            caller._thread.join(timeout=1.0)


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


def test_receipt_stdio_bounded_request_times_out_without_poisoning_session(
    monkeypatch,
) -> None:
    caller = StdioMCPReceiptToolCaller(frozenset({"first", "second"}))

    class _Session:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> _MCPResponse:
            if name == "first":
                await anyio.sleep_forever()
            return _MCPResponse('{"ok": true}')

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(
        mcp.client.stdio,
        "stdio_client",
        lambda parameters: _AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        mcp.client.stdio, "StdioServerParameters", lambda **kwargs: kwargs
    )

    try:
        with pytest.raises(TimeoutError):
            caller.call_with_timeout("first", {}, timeout=0.01)
        assert caller("second", {}) == {"ok": True}
    finally:
        caller.close()


def test_receipt_stdio_bounded_request_expires_while_queued(monkeypatch) -> None:
    caller = StdioMCPReceiptToolCaller(frozenset({"first", "second", "third"}))
    first_entered = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    class _Session:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def initialize(self) -> None:
            pass

        async def call_tool(
            self, name: str, arguments: dict[str, object]
        ) -> _MCPResponse:
            calls.append(name)
            if name == "first":
                first_entered.set()
                while not release_first.is_set():
                    await anyio.sleep(0.01)
            return _MCPResponse(f'{{"name": "{name}"}}')

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(
        mcp.client.stdio,
        "stdio_client",
        lambda parameters: _AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        mcp.client.stdio, "StdioServerParameters", lambda **kwargs: kwargs
    )

    first_result: Future[object] = Future()

    def first_request() -> None:
        try:
            first_result.set_result(caller("first", {}))
        except BaseException as error:  # noqa: BLE001 - relay the worker outcome
            first_result.set_exception(error)

    worker = threading.Thread(target=first_request, daemon=True)
    worker.start()
    try:
        assert first_entered.wait(timeout=1.0)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            caller.call_with_timeout("second", {}, timeout=0.05)
        assert time.monotonic() - started < 0.5

        release_first.set()
        assert first_result.result(timeout=1.0) == {"name": "first"}
        assert caller("third", {}) == {"name": "third"}
        assert calls == ["first", "third"]
    finally:
        release_first.set()
        worker.join(timeout=1.0)
        caller.close()


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
    caller = StdioMCPReceiptToolCaller(RECEIPT_TOOLS)
    result: Future[object] = Future()
    requests = [("add_tags", {}, result, None), None]

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


def _guarded_caller(
    monkeypatch,
    allowed_tools: frozenset[str],
    respond: Callable[[str, dict[str, Any]], Any] | None = None,
) -> tuple[StdioMCPReceiptToolCaller, list[tuple[str, dict[str, Any]]]]:
    """A real allowlisted transport whose MCP session records what reaches it.

    The recording is the point: a refusal must be observable as *nothing
    dispatched*, not merely as an exception raised somewhere downstream of a
    call that already left the process.
    """

    dispatched: list[tuple[str, dict[str, Any]]] = []

    class _Session:
        def __init__(self, read: object, write: object) -> None:
            pass

        async def initialize(self) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, Any]) -> _MCPResponse:
            dispatched.append((name, arguments))
            payload = respond(name, arguments) if respond else {"ok": True}
            return _MCPResponse(json.dumps(payload))

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(mcp, "ClientSession", _Session)
    monkeypatch.setattr(
        mcp.client.stdio,
        "stdio_client",
        lambda parameters: _AsyncContext((object(), object())),
    )
    monkeypatch.setattr(
        mcp.client.stdio, "StdioServerParameters", lambda **kwargs: kwargs
    )
    return StdioMCPReceiptToolCaller(allowed_tools), dispatched


def test_the_write_transport_cannot_be_built_without_an_allowlist() -> None:
    """The privilege has to be stated at construction, every single time."""

    with pytest.raises(TypeError):
        StdioMCPReceiptToolCaller()  # type: ignore[call-arg]
    # A mutable set would let any holder of the reference widen the privilege
    # after construction, which is the same hole one indirection further out.
    with pytest.raises(TypeError, match="frozenset"):
        StdioMCPReceiptToolCaller({"add_tags"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        StdioMCPReceiptToolCaller(frozenset())


def test_receipt_scoped_transport_refuses_the_repair_tools(monkeypatch) -> None:
    """A receipt session may not write terms or owners on anyone's assets."""

    caller, dispatched = _guarded_caller(monkeypatch, RECEIPT_TOOLS)
    try:
        for tool in ("add_terms", "add_owners"):
            with pytest.raises(ToolNotAllowed, match=tool):
                caller(tool, {"entity_urns": [URN]})
        assert dispatched == []
        assert caller._thread is None

        # `add_tags` stays reachable because the verdict badge is written with
        # it; what keeps that write inside Sidq's namespace is the badge URN,
        # not the tool name.
        assert caller("add_tags", {"tag_urns": ["urn:li:tag:sidq:verified"]}) == {
            "ok": True
        }
        assert [name for name, _ in dispatched] == ["add_tags"]
    finally:
        caller.close()


def test_repair_scoped_transport_accepts_only_the_proposal_tools(monkeypatch) -> None:
    """The repair session gets exactly what `propose` can emit, and no more."""

    caller, dispatched = _guarded_caller(monkeypatch, REPAIR_TOOLS)
    try:
        for tool in ("add_tags", "add_terms", "add_owners"):
            assert caller(tool, {"entity_urns": [URN]}) == {"ok": True}
        assert [name for name, _ in dispatched] == [
            "add_tags",
            "add_terms",
            "add_owners",
        ]

        for tool in ("save_document", "add_structured_properties", "remove_tags"):
            with pytest.raises(ToolNotAllowed, match=tool):
                caller(tool, {"entity_urns": [URN]})
        assert len(dispatched) == 3
    finally:
        caller.close()


def test_an_unknown_tool_never_reaches_the_session_in_any_scope(monkeypatch) -> None:
    """Fail closed: an unlisted name is refused before a transport exists."""

    for allowed in (RECEIPT_TOOLS, RECEIPT_READ_TOOLS, REPAIR_TOOLS):
        caller, dispatched = _guarded_caller(monkeypatch, allowed)
        try:
            with pytest.raises(ToolNotAllowed, match="delete_entities"):
                caller("delete_entities", {"urns": [URN]})
            with pytest.raises(ToolNotAllowed, match="delete_entities"):
                caller.call_with_timeout("delete_entities", {"urns": [URN]}, timeout=5)
            assert dispatched == []
            assert caller._thread is None
        finally:
            caller.close()


def test_the_receipt_write_path_completes_through_the_allowlisted_transport(
    monkeypatch,
) -> None:
    """Every tool `write_receipt` uses is on the receipt list, in write order."""

    receipt = build_receipt(
        URN, _verdict(), checked_at=datetime(2026, 8, 2, tzinfo=UTC)
    )
    properties: dict[str, list[str]] = {}
    tags: set[str] = set()

    def respond(name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_entities":
            return {
                "entities": [
                    {
                        "urn": URN,
                        "globalTags": {"tags": [{"tag": {"urn": urn}} for urn in tags]},
                        "structuredProperties": {
                            "properties": [
                                {
                                    "structuredProperty": {"urn": urn},
                                    "values": [
                                        {"stringValue": value} for value in values
                                    ],
                                }
                                for urn, values in properties.items()
                            ]
                        },
                    }
                ]
            }
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
        if name == "add_structured_properties":
            properties.update(arguments["property_values"])
        if name == "add_tags":
            tags.update(arguments["tag_urns"])
        return {"success": True, "urn": "urn:li:document:sidq-receipt"}

    caller, dispatched = _guarded_caller(monkeypatch, RECEIPT_TOOLS, respond)
    try:
        written = write_receipt(receipt, caller)
    finally:
        caller.close()

    # Document first, badge second, structured properties last — the order the
    # allowlist must not disturb — then the direct confirmation read.
    assert [name for name, _ in dispatched] == [
        "get_entities",
        "get_lineage",
        "get_lineage",
        "save_document",
        "add_tags",
        "add_structured_properties",
        "get_entities",
    ]
    assert {name for name, _ in dispatched} <= RECEIPT_TOOLS
    assert written["confirmed"] is True
    assert written["receipt"]["evidence_url"] == "urn:li:document:sidq-receipt"


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
