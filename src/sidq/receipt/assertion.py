"""Publish opt-in Sidq verdict reports as native DataHub assertions.

The DataHub MCP mutation surface has no assertion tool.  Like receipt bootstrap,
this boundary uses the supported DataHub SDK for the capability MCP cannot
express; receipt values themselves remain the responsibility of the official
MCP tools.

Each assertion URN is a hash of a dataset URN and Sidq rule id, so re-running
updates the same rule on the same dataset rather than duplicating it.  Each run
event reports one rule by that rule's own severity: ``warn`` and ``block`` did
not pass and take ``FAILURE``, and the native results carry both the rule's
severity and the receipt-wide verdict so a reader can tell which is which.

Two limits are not solved here and are documented in ``docs/RECEIPT-SPEC.md``
rather than hidden.  A rule that stops firing keeps its last assertion, because
nothing retires one; and a ``WARN`` shows in DataHub's aggregate quality chip
as failing, since that surface has no third state to render.
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

    # The real imports, not `find_spec("datahub")`. A present but wrong SDK
    # version satisfies a spec lookup and then fails on a renamed aspect class
    # at emission — after the budget is spent and the receipts are written,
    # which is the exact sequence this precondition exists to prevent.
    try:
        import datahub.emitter.mcp
        import datahub.ingestion.graph.client
        import datahub.ingestion.graph.config  # noqa: F401
        from datahub.metadata.schema_classes import (  # noqa: F401
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


def assertion_urn(dataset_urn: str, rule_id: str) -> str:
    """Return the stable native assertion URN for one Sidq rule evaluation."""

    digest = hashlib.sha256(f"{dataset_urn}\0{rule_id}".encode()).hexdigest()
    return f"urn:li:assertion:sidq-{digest}"


def assertion_result_type(verdict: str) -> str:
    """Map a Sidq verdict onto the DataHub result type that reports it.

    ``AssertionResultType`` also carries ``INIT`` and ``ERROR``, but those
    describe a run that started or could not complete. Every verdict Sidq
    publishes is a completed evaluation, so only the two outcome values apply,
    and ``WARN`` takes ``FAILURE`` because the policy condition did not pass.
    """

    if verdict == "PASS":
        return "SUCCESS"
    if verdict in {"WARN", "BLOCK"}:
        return "FAILURE"
    raise ValueError(f"unsupported Sidq verdict: {verdict}")


def assertion_result_for_severity(severity: str) -> str:
    """Report one rule by ITS OWN severity, not by the receipt's verdict.

    A BLOCK receipt can carry findings that did not block: ``info`` evidence is
    context Sidq recorded, not a condition that failed. Publishing the
    receipt-level verdict against every rule would paint each of them red in
    the catalog's quality surface and state, of a rule that passed, that it
    failed.
    """

    return "FAILURE" if severity.lower() in {"warn", "block"} else "SUCCESS"


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
        # DATAHUB_GMS_URL and nothing else, because that is the variable the
        # MCP server process reads to reach the catalog the receipts were just
        # written to. Defaulting to localhost here once meant a run against a
        # remote catalog could write its receipts there and its assertions into
        # whatever happened to be listening locally — two catalogs, one report
        # of success. An unset variable is refused rather than guessed.
        target = gms_url or os.environ.get("DATAHUB_GMS_URL", "")
        if not target:
            raise DataHubSDKUnavailable(
                "cannot mirror assertions without DATAHUB_GMS_URL: it names the "
                "catalog the receipts were written to, and guessing a default "
                "risks writing them into a different one"
            )
        graph = DataHubGraph(
            DatahubClientConfig(
                server=target,
                token=os.environ.get("DATAHUB_GMS_TOKEN"),
            )
        )

    created: list[str] = []
    existing: list[str] = []
    runs: list[str] = []
    try:
        for receipt in receipts:
            for rule_id, severity, summary, logic in _rule_reports(receipt):
                urn = assertion_urn(receipt.urn, rule_id)
                # Emitted every run, not only the first. The logic string
                # carries this run's evidence summary, so skipping the write
                # when the assertion already exists would leave the catalog
                # displaying the first run's reasoning forever while the run
                # events beneath it said something else. `created` and
                # `existing` still distinguish a new assertion from an updated
                # one; both are written.
                current = graph.get_aspect(urn, AssertionInfoClass)
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
                            # DataHub lists this as the assertion's name, so it
                            # is the short rule identity; the URN and the
                            # reasoning live in the entity and logic fields.
                            description=f"Sidq policy rule {rule_id}",
                        ),
                    )
                )
                (existing if current is not None else created).append(urn)

                native_results = _native_results(receipt, rule_id, severity, summary)
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
                                    assertion_result_type(receipt.verdict)
                                    if not severity
                                    else assertion_result_for_severity(severity),
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


def _rule_reports(receipt: Receipt) -> Iterable[tuple[str, str, str, str]]:
    """Yield ``(rule_id, severity, summary, logic)`` per rule in the receipt.

    Messages keep the order the engine recorded them, because that order is
    part of the finding and reordering it would publish a sentence Sidq never
    wrote. A rule's severity is the strongest one its findings carry, so a rule
    that blocked once is not softened by also having recorded context.
    """

    grouped: dict[str, list[str]] = {}
    severities: dict[str, str] = {}
    rank = {"info": 0, "warn": 1, "block": 2}
    for item in receipt.evidence:
        rule_id = item.get("rule_id")
        message = item.get("message")
        severity = item.get("severity")
        if not isinstance(rule_id, str) or not rule_id:
            continue
        grouped.setdefault(rule_id, []).append(
            message if isinstance(message, str) and message else rule_id
        )
        severity = severity.lower() if isinstance(severity, str) else ""
        held = severities.get(rule_id, "")
        if rank.get(severity, -1) > rank.get(held, -1):
            severities[rule_id] = severity
    if not grouped:
        # No per-rule evidence to report, so the whole verdict is reported
        # once. Its severity is empty; the caller falls back to the receipt
        # verdict, which is the only statement available.
        yield (
            _FALLBACK_RULE_ID,
            "",
            "No individual rule evidence was recorded.",
            (
                "Sidq compared the dataset's collected catalog evidence with its "
                "policy and recorded the resulting verdict."
            ),
        )
        return
    for rule_id in sorted(grouped):
        seen: dict[str, None] = dict.fromkeys(grouped[rule_id])
        summary = " ".join(seen)
        yield (
            rule_id,
            severities.get(rule_id, ""),
            summary,
            (f"Sidq rule {rule_id}: {summary}"),
        )


def _native_results(
    receipt: Receipt, rule_id: str, severity: str, summary: str
) -> dict[str, str]:
    return {
        "sidq.verdict": receipt.verdict,
        "sidq.rule_id": rule_id,
        # The rule's own severity, next to the receipt-wide verdict, so a
        # reader can see which of the two this row is reporting.
        "sidq.severity": severity or "verdict",
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
