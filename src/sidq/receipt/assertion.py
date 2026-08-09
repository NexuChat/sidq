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

A rule that stops firing is retired rather than left claiming a failure: the
next run emits one closing run event for it, so the catalog stops stating
something Sidq no longer holds.  Assertions Sidq did not write, and ones an
operator soft-deleted, are left alone.

One limit is not solved and is documented in ``docs/RECEIPT-SPEC.md`` rather
than hidden: a ``WARN`` is counted with the failures in DataHub's aggregate
quality chip, because that surface has no third state to render.
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
# Written as the assertion's name and read back to recover the rule it reports,
# so retirement can name a rule it did not evaluate this run. One constant,
# used by both directions, is what keeps that round trip honest.
_NAME_PREFIX = "Sidq policy rule "
_CUSTOM_TYPE = "sidq.policy_rule"
_RETIRED_SUMMARY = "This rule did not fire in the latest Sidq evaluation."

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
            StatusClass,
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
        return {"created": (), "existing": (), "runs": (), "retired": ()}

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
            StatusClass,
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
    retired: list[str] = []

    def _run_event(
        urn: str, receipt: Receipt, results: dict[str, str], result_type: str
    ) -> str:
        run_id = _run_id(urn, results)
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
                        type=getattr(AssertionResultTypeClass, result_type),
                        nativeResults=results,
                        externalUrl=receipt.evidence_url or None,
                    ),
                ),
            )
        )
        return run_id

    try:
        for receipt in receipts:
            emitted: set[str] = set()
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
                                type=_CUSTOM_TYPE,
                                entity=receipt.urn,
                                logic=logic,
                            ),
                            source=AssertionSourceClass(
                                type=AssertionSourceTypeClass.EXTERNAL
                            ),
                            # DataHub lists this as the assertion's name, so it
                            # is the short rule identity; the URN and the
                            # reasoning live in the entity and logic fields.
                            description=f"{_NAME_PREFIX}{rule_id}",
                        ),
                    )
                )
                (existing if current is not None else created).append(urn)

                runs.append(
                    _run_event(
                        urn,
                        receipt,
                        _native_results(receipt, rule_id, severity, summary),
                        assertion_result_type(receipt.verdict)
                        if not severity
                        else assertion_result_for_severity(severity),
                    )
                )
                emitted.add(urn)

            # A rule Sidq no longer reports must stop claiming a failure. The
            # catalog keeps every assertion ever written, so without this a
            # fixed problem stays red beside the run that fixed it, and the
            # surface this feature exists to serve states something Sidq no
            # longer holds. Retiring means one more honest run event, not a
            # deletion: the history stays readable.
            for stale, stale_rule in _ours_not_emitted(
                graph, receipt.urn, emitted, AssertionInfoClass, StatusClass
            ):
                runs.append(
                    _run_event(
                        stale,
                        receipt,
                        _native_results(
                            receipt, stale_rule, "retired", _RETIRED_SUMMARY
                        ),
                        "SUCCESS",
                    )
                )
                retired.append(stale)
    finally:
        if owns_graph:
            close = getattr(graph, "close", None)
            if callable(close):
                close()
    return {
        "created": tuple(created),
        "existing": tuple(existing),
        "runs": tuple(runs),
        "retired": tuple(retired),
    }


def _ours_not_emitted(
    graph: Any,
    dataset_urn: str,
    emitted: set[str],
    info_class: Any,
    status_class: Any,
) -> Iterable[tuple[str, str]]:
    """Yield ``(assertion_urn, rule_id)`` for Sidq rules absent from this run.

    Only assertions Sidq itself wrote are considered: a dbt or Snowflake check
    on the same dataset is somebody else's statement, and retiring it would be
    Sidq overwriting a claim it never made.

    Soft-deleted ones are skipped too. DataHub keeps the Asserts relationship
    after a soft delete, so retiring one would emit a fresh run event and pull
    an assertion the operator removed back into their Quality tab. Measured on
    2026-08-09: without this filter that is exactly what happened.
    """

    related = graph.get_related_entities(
        dataset_urn, ["Asserts"], graph.RelationshipDirection.INCOMING
    )
    for entity in related:
        urn = entity.urn
        if urn in emitted:
            continue
        status = graph.get_aspect(urn, status_class)
        if getattr(status, "removed", False):
            continue
        info = graph.get_aspect(urn, info_class)
        custom = getattr(info, "customAssertion", None)
        if getattr(custom, "type", None) != _CUSTOM_TYPE:
            continue
        description = getattr(info, "description", "") or ""
        if not description.startswith(_NAME_PREFIX):
            continue
        yield urn, description[len(_NAME_PREFIX) :]


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
