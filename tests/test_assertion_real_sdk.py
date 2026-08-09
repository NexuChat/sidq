"""Exercise the assertion emitter against the real DataHub aspect classes.

Every other assertion test runs against a contract-checked stand-in, because
`acryl-datahub` cannot live in the project environment: it resolves `pydantic`
below the 2.12 that Sidq's `mcp>=2` declares. That stand-in verifies field
*names* against a committed contract, which is what catches a typo or an
upstream rename — but it cannot catch a wrong *value*, because it stores
whatever it is handed.

These tests close that. They import the real aspect classes, let
`emit_assertions` build them, and serialise the result through the SDK's own
`to_obj()`, so the values Sidq would put on the wire are the values asserted
here. Only the graph is a stub: no catalog, no network, no credentials.

The whole module skips where no SDK is installed, which is every environment
except `make sdk-contract`. That is the same division as the contract test: the
project venv runs what needs no SDK, the container runs what does, and neither
reports an unavailable check as a passing one.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from sidq.models import Finding, Verdict
from sidq.receipt.assertion import assertion_urn, emit_assertions
from sidq.receipt.build import build_receipt

# Marked rather than skipped at import, so every test here is still collected
# and each reports its own skip. A module that vanishes from the count is a
# check nobody can see was not run.
_HAS_SDK = importlib.util.find_spec("datahub") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_SDK,
    reason="acryl-datahub is not installed here; see docs/RECEIPT-SPEC.md",
)

if _HAS_SDK:
    from datahub.metadata.schema_classes import (
        AssertionInfoClass,
        AssertionRunEventClass,
        StatusClass,
    )

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.public.orders,PROD)"
CHECKED_AT = datetime(2026, 8, 2, 11, 4, tzinfo=UTC)


class _StubGraph:
    """A catalog that records proposals instead of sending them.

    Deliberately not a mock of the aspects: the aspects are real, so an invalid
    one raises here exactly as it would against a live GMS.
    """

    class RelationshipDirection:
        INCOMING = "INCOMING"

    def __init__(self) -> None:
        self.infos: dict[str, Any] = {}
        self.events: list[Any] = []
        self.asserts: dict[str, list[str]] = {}
        self.removed: set[str] = set()

    def get_aspect(self, urn: str, aspect_type: Any) -> Any:
        if aspect_type is StatusClass:
            return StatusClass(removed=True) if urn in self.removed else None
        return self.infos.get(urn)

    def get_related_entities(
        self, urn: str, relationships: list[str], direction: str
    ) -> list[Any]:
        return [SimpleNamespace(urn=item) for item in sorted(self.asserts.get(urn, []))]

    def emit_mcp(self, mcp: Any) -> None:
        aspect = mcp.aspect
        if isinstance(aspect, AssertionInfoClass):
            self.infos[mcp.entityUrn] = aspect
            self.asserts.setdefault(aspect.customAssertion.entity, [])
            if mcp.entityUrn not in self.asserts[aspect.customAssertion.entity]:
                self.asserts[aspect.customAssertion.entity].append(mcp.entityUrn)
        else:
            assert isinstance(aspect, AssertionRunEventClass)
            self.events.append(aspect)


def _receipt(*findings: Finding, verdict: str = "BLOCK") -> Any:
    return build_receipt(
        URN,
        Verdict(verdict, None, findings, (), "a" * 40, "sha256:policy"),
        checked_at=CHECKED_AT,
    )


def test_the_emitted_aspects_serialise_through_the_real_sdk() -> None:
    """What Sidq builds must be a valid aspect, not merely an object with fields.

    `to_obj()` is the SDK's own serialisation, so this asserts the payload that
    would reach a catalog rather than the arguments that were passed in.
    """
    graph = _StubGraph()
    blocking = Finding("critical_downstream", "block", "Nine owners depend.", ())

    result = emit_assertions([_receipt(blocking)], graph)

    urn = assertion_urn(URN, "critical_downstream")
    assert result["created"] == (urn,)

    info = graph.infos[urn].to_obj()
    assert info["type"] == "CUSTOM"
    assert info["source"]["type"] == "EXTERNAL"
    assert info["description"] == "Sidq policy rule critical_downstream"
    assert info["customAssertion"]["type"] == "sidq.policy_rule"
    assert info["customAssertion"]["entity"] == URN

    event = graph.events[0].to_obj()
    assert event["asserteeUrn"] == URN
    assert event["assertionUrn"] == urn
    assert event["status"] == "COMPLETE"
    assert event["timestampMillis"] == 1785668640000
    assert event["result"]["type"] == "FAILURE"
    assert event["result"]["nativeResults"]["sidq.severity"] == "block"
    assert event["result"]["nativeResults"]["sidq.verdict"] == "BLOCK"
    # A stable message id is what lets DataHub replace the event on a retry
    # rather than accumulate one per run.
    assert event["messageId"] == event["runId"]


def test_a_rule_that_did_not_block_is_not_published_as_a_failure() -> None:
    """The severity mapping, asserted on the real result enum rather than a string."""

    graph = _StubGraph()
    blocking = Finding("critical_downstream", "block", "Nine owners depend.", ())
    context = Finding("pii_marker", "info", "The field is marked PII.", ())

    emit_assertions([_receipt(blocking, context)], graph)

    published = {
        event.result.nativeResults["sidq.rule_id"]: event.to_obj()["result"]["type"]
        for event in graph.events
    }
    assert published == {"critical_downstream": "FAILURE", "pii_marker": "SUCCESS"}


def test_a_warning_is_named_as_one_and_still_reports_failure() -> None:
    graph = _StubGraph()
    warned = Finding("wide_blast_radius", "warn", "Sixteen consumers.", ())

    emit_assertions([_receipt(warned, verdict="WARN")], graph)

    urn = assertion_urn(URN, "wide_blast_radius")
    assert (
        graph.infos[urn].to_obj()["description"]
        == "Sidq policy rule wide_blast_radius (warning)"
    )
    assert graph.events[0].to_obj()["result"]["type"] == "FAILURE"


def test_retirement_and_soft_delete_hold_against_the_real_classes() -> None:
    """The two behaviours that read aspects back, not just write them.

    Retirement looks up `AssertionInfo` and `Status` on entities the graph
    reports; a stand-in could return anything for those, so this is where real
    classes earn their place.
    """
    graph = _StubGraph()
    fired = Finding("critical_downstream", "block", "Nine owners depend.", ())
    emit_assertions([_receipt(fired)], graph)
    urn = assertion_urn(URN, "critical_downstream")

    retired = emit_assertions([_receipt(verdict="PASS")], graph)

    assert retired["retired"] == (urn,)
    closing = graph.events[-1].to_obj()
    assert closing["result"]["type"] == "SUCCESS"
    assert closing["result"]["nativeResults"]["sidq.severity"] == "retired"
    # Retiring rewrites the definition, so the row stops showing the reasoning
    # of a run that no longer holds.
    assert graph.infos[urn].to_obj()["customAssertion"]["logic"] == (
        "This rule did not fire in the latest Sidq evaluation."
    )

    # Retired once, not on every later run.
    assert emit_assertions([_receipt(verdict="PASS")], graph)["retired"] == ()

    # And an operator's deletion is not reversed when the rule fires again.
    graph.removed.add(urn)
    again = emit_assertions([_receipt(fired)], graph)
    assert again["skipped"] == (urn,)
    assert again["created"] == () and again["existing"] == ()
