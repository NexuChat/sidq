"""Publish opt-in Sidq verdict reports as native DataHub assertions.

The DataHub MCP mutation surface has no assertion tool.  Like receipt bootstrap,
this boundary uses the supported DataHub SDK for the capability MCP cannot
express; receipt values themselves remain the responsibility of the official
MCP tools.

Each assertion URN is a hash of a dataset URN and Sidq rule id, so the same
rule on the same dataset is updated rather than duplicated.  ``WARN`` maps to
``FAILURE``: DataHub assertion results distinguish only success and failure,
and a warning is an honestly failed policy condition even though Sidq does not
treat it as a blocking condition.  The native results retain Sidq's ``WARN``
verdict and evidence so the UI does not imply a block.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from .build import Receipt

_FALLBACK_RULE_ID = "sidq.verdict"

# Measured on 2026-08-09: acryl-datahub 1.6.0.16 resolves pydantic to 2.11.10,
# contradicting the pydantic>=2.12 that mcp 2.0.0 declares, and pip reports the
# conflict. Sidq's own MCP suites nevertheless passed in that combined
# environment, so this is an install whose declared constraints are unsatisfied
# rather than one observed to fail — which is still not something to ship as a
# supported extra, because passing today is not promised by any declared
# version. The message says that much and no more, instead of leaving a bare
# ModuleNotFoundError for the operator to decode.
_SDK_REQUIRED = (
    "the native assertion mirror needs the DataHub Python SDK, which Sidq "
    "does not install and does not offer as an extra: acryl-datahub resolves "
    "pydantic below the 2.12 that Sidq's mcp>=2 declares, so that install is "
    "resolver-inconsistent even though Sidq's MCP tests passed under it when "
    "measured. Run --write-assertions from an interpreter that already "
    "carries acryl-datahub; see docs/RECEIPT-SPEC.md. Receipts themselves "
    "need no SDK."
)


class DataHubSDKUnavailable(RuntimeError):
    """Raised when the assertion mirror runs without the DataHub SDK present."""


def require_sdk() -> None:
    """Refuse early when the SDK the mirror needs is not installed.

    Callers use this as a precondition, before spending an audit budget or
    writing anything. Discovering the absence at emission time would leave the
    operator with receipts written, assertions missing, and a failure they
    could have been told about before the first catalog read. The import inside
    ``emit_assertions`` still guards the operation itself; this only moves the
    same refusal earlier.
    """

    import importlib.util

    if importlib.util.find_spec("datahub") is None:
        raise DataHubSDKUnavailable(_SDK_REQUIRED)


def assertion_urn(dataset_urn: str, rule_id: str) -> str:
    """Return the stable native assertion URN for one Sidq rule evaluation."""

    digest = hashlib.sha256(f"{dataset_urn}\0{rule_id}".encode()).hexdigest()
    return f"urn:li:assertion:sidq-{digest}"


def assertion_result_type(verdict: str) -> str:
    """Map Sidq's three verdicts onto DataHub's binary result vocabulary."""

    if verdict == "PASS":
        return "SUCCESS"
    if verdict in {"WARN", "BLOCK"}:
        return "FAILURE"
    raise ValueError(f"unsupported Sidq verdict: {verdict}")


def emit_assertions(
    receipts: Sequence[Receipt],
    graph: Any | None = None,
    *,
    gms_url: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """Emit native assertion definitions and one run event per receipt rule.

    This is intentionally separate from receipt MCP writeback and callers must
    opt in.  Definitions and run events require the DataHub SDK because the
    official MCP server registers no assertion mutation tool.
    """

    if not receipts:
        # Every receipt write can be rejected, and then there is nothing to
        # mirror. Opening a GMS client anyway would turn a reported writeback
        # failure into a second, unrelated connection error.
        return {"created": (), "existing": (), "runs": ()}

    # One import site, so the missing-SDK boundary is stated once and cannot
    # be reported differently depending on how far the run got.
    try:
        from datahub.emitter.mcp import MetadataChangeProposalWrapper
        from datahub.ingestion.graph.client import DataHubGraph
        from datahub.ingestion.graph.config import DatahubClientConfig
        from datahub.metadata.schema_classes import (
            AssertionInfoClass,
            AssertionResultClass,
            AssertionResultTypeClass,
            AssertionRunEventClass,
            AssertionRunStatusClass,
            AssertionSourceClass,
            AssertionSourceTypeClass,
            AssertionTypeClass,
            CustomAssertionInfoClass,
        )
    except ImportError as error:
        raise DataHubSDKUnavailable(_SDK_REQUIRED) from error

    owns_graph = graph is None
    if graph is None:
        graph = DataHubGraph(
            DatahubClientConfig(
                server=gms_url
                or os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
                token=os.environ.get("DATAHUB_GMS_TOKEN"),
            )
        )

    created: list[str] = []
    existing: list[str] = []
    runs: list[str] = []
    try:
        for receipt in receipts:
            for rule_id, summary, logic in _rule_reports(receipt):
                urn = assertion_urn(receipt.urn, rule_id)
                current = graph.get_aspect(urn, AssertionInfoClass)
                if current is None:
                    graph.emit_mcp(
                        MetadataChangeProposalWrapper(
                            entityUrn=urn,
                            aspect=AssertionInfoClass(
                                type=AssertionTypeClass.CUSTOM,
                                customAssertion=CustomAssertionInfoClass(
                                    type="sidq.policy_rule",
                                    entity=receipt.urn,
                                    logic=logic,
                                ),
                                source=AssertionSourceClass(
                                    type=AssertionSourceTypeClass.EXTERNAL
                                ),
                                description=(
                                    f"Sidq rule {rule_id} evaluates catalog evidence "
                                    f"for {receipt.urn}."
                                ),
                            ),
                        )
                    )
                    created.append(urn)
                else:
                    existing.append(urn)

                native_results = _native_results(receipt, rule_id, summary)
                run_id = _run_id(urn, native_results)
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=urn,
                        aspect=AssertionRunEventClass(
                            timestampMillis=_checked_at_millis(receipt.checked_at),
                            runId=run_id,
                            asserteeUrn=receipt.urn,
                            status=AssertionRunStatusClass.COMPLETE,
                            assertionUrn=urn,
                            messageId=run_id,
                            result=AssertionResultClass(
                                type=getattr(
                                    AssertionResultTypeClass,
                                    assertion_result_type(receipt.verdict),
                                ),
                                nativeResults=native_results,
                                externalUrl=receipt.evidence_url or None,
                            ),
                        ),
                    )
                )
                runs.append(run_id)
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()
    return {
        "created": tuple(created),
        "existing": tuple(existing),
        "runs": tuple(runs),
    }


def _rule_reports(receipt: Receipt) -> Iterable[tuple[str, str, str]]:
    grouped: dict[str, list[str]] = {}
    for item in receipt.evidence:
        rule_id = item.get("rule_id")
        message = item.get("message")
        if isinstance(rule_id, str) and rule_id:
            grouped.setdefault(rule_id, []).append(
                message if isinstance(message, str) and message else rule_id
            )
    if not grouped:
        yield (
            _FALLBACK_RULE_ID,
            "No individual rule evidence was recorded.",
            (
                "Sidq compared the dataset's collected catalog evidence with its "
                "policy and recorded the resulting verdict."
            ),
        )
        return
    for rule_id in sorted(grouped):
        summary = " ".join(sorted(set(grouped[rule_id])))
        yield rule_id, summary, f"Sidq rule {rule_id}: {summary}"


def _native_results(receipt: Receipt, rule_id: str, summary: str) -> dict[str, str]:
    return {
        "sidq.verdict": receipt.verdict,
        "sidq.rule_id": rule_id,
        "sidq.policy_hash": receipt.policy_hash,
        "sidq.commit_sha": receipt.commit_sha,
        "sidq.checked_at": receipt.checked_at,
        "sidq.evidence_summary": summary,
    }


def _run_id(assertion: str, native_results: dict[str, str]) -> str:
    encoded = json.dumps(
        {"assertion": assertion, "native_results": native_results},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sidq-{hashlib.sha256(encoded.encode()).hexdigest()}"


def _checked_at_millis(checked_at: str) -> int:
    timestamp = datetime.fromisoformat(checked_at)
    return int(timestamp.astimezone(UTC).timestamp() * 1000)
