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

A rule that stops firing is retired rather than left claiming a failure: one
closing run event, and the definition rewritten so the row stops displaying the
last firing's reasoning.  Retiring happens once, not every run afterwards.

An assertion Sidq did not write is left alone, and so is one an operator
soft-deleted -- on both paths.  Re-creating a deleted row because its rule fired
again would overrule a decision with a metadata write nobody asked for, so the
run reports the skip instead.

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
# Every URN Sidq mints starts here, so a foreign assertion is rejected without
# spending a single aspect read on it.
_URN_PREFIX = "urn:li:assertion:sidq-"
_RETIRED_SUMMARY = "This rule did not fire in the latest Sidq evaluation."
# DataHub's quality chip counts passing against failing with no third state, so
# a WARN lands among the failures whatever Sidq does. What Sidq can still do is
# stop a warning from reading as a block in the row a human actually looks at.
_WARNING_SUFFIX = " (warning)"

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
    return f"{_URN_PREFIX}{digest}"


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
        return {
            "created": (),
            "existing": (),
            "runs": (),
            "retired": (),
            "skipped": (),
        }

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
    skipped: list[str] = []

    def _definition(dataset_urn: str, name: str, logic: str) -> Any:
        return AssertionInfoClass(
            type=AssertionTypeClass.CUSTOM,
            customAssertion=CustomAssertionInfoClass(
                type=_CUSTOM_TYPE, entity=dataset_urn, logic=logic
            ),
            source=AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL),
            # DataHub lists this as the assertion's name, so it is the short
            # rule identity; the URN and the reasoning live in the entity and
            # logic fields.
            description=name,
        )

    def _run_event(
        urn: str,
        receipt: Receipt,
        results: dict[str, str],
        result_type: str,
        *,
        evidence_url: str = "",
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
                        externalUrl=evidence_url or None,
                    ),
                ),
            )
        )
        return run_id

    try:
        # Accumulated across the whole call, not per receipt: two receipts for
        # one dataset would otherwise each retire the other's rules.
        emitted: dict[str, set[str]] = {}
        last: dict[str, Receipt] = {}
        for receipt in receipts:
            seen = emitted.setdefault(receipt.urn, set())
            last[receipt.urn] = receipt
            for rule_id, severity, summary, logic in _rule_reports(receipt):
                urn = assertion_urn(receipt.urn, rule_id)
                current = graph.get_aspect(urn, AssertionInfoClass)
                if _is_removed(graph, urn, StatusClass):
                    # The operator deleted this row. Writing it again would
                    # overrule that with a metadata write they never asked for,
                    # so the run reports the skip instead of quietly reversing
                    # the decision.
                    skipped.append(urn)
                    seen.add(urn)
                    continue
                # Emitted every run, not only the first. The logic string
                # carries this run's evidence summary, so skipping the write
                # when the assertion already exists would leave the catalog
                # displaying the first run's reasoning forever while the run
                # events beneath it said something else.
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=urn,
                        aspect=_definition(
                            receipt.urn, _assertion_name(rule_id, severity), logic
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
                        evidence_url=receipt.evidence_url,
                    )
                )
                seen.add(urn)

        # A rule Sidq no longer reports must stop claiming a failure. The
        # catalog keeps every assertion ever written, so without this a fixed
        # problem stays red beside the run that fixed it, and the surface this
        # feature exists to serve states something Sidq no longer holds.
        # Retiring means one more honest run event, not a deletion.
        for dataset_urn, seen in emitted.items():
            receipt = last[dataset_urn]
            for stale, stale_rule in _ours_not_emitted(
                graph, dataset_urn, seen, AssertionInfoClass, StatusClass
            ):
                # The definition is rewritten too, so the row stops displaying
                # the last firing's reasoning and any "(warning)" in its name,
                # and so the next run can see it is already retired instead of
                # closing it again every run for the life of the dataset.
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=stale,
                        aspect=_definition(
                            dataset_urn,
                            f"{_NAME_PREFIX}{stale_rule}",
                            _RETIRED_SUMMARY,
                        ),
                    )
                )
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
        "skipped": tuple(skipped),
    }


def _is_removed(graph: Any, urn: str, status_class: Any) -> bool:
    return bool(getattr(graph.get_aspect(urn, status_class), "removed", False))


def _ours_not_emitted(
    graph: Any,
    dataset_urn: str,
    emitted: set[str],
    info_class: Any,
    status_class: Any,
) -> Iterable[tuple[str, str]]:
    """Yield ``(assertion_urn, rule_id)`` for Sidq rules absent from this run.

    Four things are filtered out, cheapest test first because a dbt-managed
    dataset can carry hundreds of assertions and each aspect read is a round
    trip.

    A URN Sidq did not mint cannot be one of ours, and that is free to check.
    A dbt or Snowflake check that survives the prefix test is still somebody
    else's statement, which the custom type settles. A soft-deleted assertion
    stays deleted: DataHub keeps the Asserts relationship after a soft delete,
    so retiring one would pull a row the operator removed back into their
    Quality tab — measured on 2026-08-09, without this filter that is exactly
    what happened. And one already retired is left alone, or a rule that
    stopped firing would collect a closing event every run forever.
    """

    related = graph.get_related_entities(
        dataset_urn, ["Asserts"], graph.RelationshipDirection.INCOMING
    )
    for entity in related:
        urn = entity.urn
        if urn in emitted or not urn.startswith(_URN_PREFIX):
            continue
        if _is_removed(graph, urn, status_class):
            continue
        info = graph.get_aspect(urn, info_class)
        custom = getattr(info, "customAssertion", None)
        if getattr(custom, "type", None) != _CUSTOM_TYPE:
            continue
        if getattr(custom, "logic", None) == _RETIRED_SUMMARY:
            continue
        description = getattr(info, "description", "") or ""
        if not description.startswith(_NAME_PREFIX):
            continue
        rule_id = _rule_id_from_name(dataset_urn, urn, description)
        # The whole-verdict row is not a rule that can stop firing; a dataset
        # alternating between clean and dirty runs would otherwise retire and
        # recreate it forever.
        if rule_id == _FALLBACK_RULE_ID:
            continue
        yield urn, rule_id


def _assertion_name(rule_id: str, severity: str) -> str:
    """Name the assertion so a warning does not read as a block.

    The aggregate chip cannot be fixed from here. The row can: a ``warn`` rule
    says so in the one place the Assertions list actually renders.
    """

    if severity.lower() == "warn":
        return f"{_NAME_PREFIX}{rule_id}{_WARNING_SUFFIX}"
    return f"{_NAME_PREFIX}{rule_id}"


def _rule_id_from_name(dataset_urn: str, urn: str, description: str) -> str:
    """Recover the rule id a name was built from, checked against the URN.

    Stripping the warning suffix by text alone would mangle a rule genuinely
    named ``x (warning)``. The URN is a hash of the dataset and the rule id, so
    it settles which reading is right rather than leaving it to a guess.
    """

    named = description[len(_NAME_PREFIX) :]
    if named.endswith(_WARNING_SUFFIX):
        stripped = named[: -len(_WARNING_SUFFIX)]
        if assertion_urn(dataset_urn, stripped) == urn:
            return stripped
    return named


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
