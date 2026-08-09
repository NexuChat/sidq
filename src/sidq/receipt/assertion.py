"""Publish opt-in Sidq verdict reports as native DataHub assertions.

This boundary uses DataHub's documented, authorization-checked GraphQL
custom-assertion API. It has zero extra dependencies and therefore runs from
Sidq's project environment rather than requiring the acryl-datahub SDK. The
MCP server (0.6.0, 21 tools) has no assertion write tool, which is why this
one writeback surface uses GraphQL rather than MCP.

Measured DataHub behavior derives a run id from ``timestampMillis`` and
deduplicates reports at the same timestamp, so retrying one receipt updates
its event rather than duplicating it. Operator soft-deletes are respected on
both emission and retirement, and a no-longer-firing rule is retired once
rather than on every later run.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
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

_MIRROR_CONFIG_REQUIRED = (
    "cannot mirror assertions without DATAHUB_GMS_URL: it names the catalog "
    "the receipts were written to, and guessing a default risks writing "
    "assertions into a different catalog"
)

_UPSERT_CUSTOM_ASSERTION = """
mutation($u:String,$i:UpsertCustomAssertionInput!){
  upsertCustomAssertion(urn:$u, input:$i){ urn }
}
"""
_REPORT_ASSERTION_RESULT = """
mutation($u:String!,$r:AssertionResultInput!){
  reportAssertionResult(urn:$u, result:$r)
}
"""
_DATASET_ASSERTIONS = """
query($u:String!,$start:Int!,$count:Int!){
  dataset(urn:$u){
    assertions(start:$start,count:$count){
      total
      assertions { urn info { description customAssertion { type logic } } }
    }
  }
}
"""
_ASSERTIONS_PAGE = 500


class AssertionMirrorUnavailable(RuntimeError):
    """Raised when assertion mirroring lacks its required catalog configuration."""


def require_mirror_config() -> None:
    """Refuse early when the target catalog cannot be identified safely.

    Callers use this before spending an audit budget or writing receipts. The
    URL is the catalog identity shared by the MCP receipt process and this
    GraphQL writeback boundary; guessing it would make one run write its two
    kinds of output to different catalogs.
    """

    if not os.environ.get("DATAHUB_GMS_URL"):
        raise AssertionMirrorUnavailable(_MIRROR_CONFIG_REQUIRED)


class _GmsTransport:
    """Small stdlib transport for the two DataHub endpoints this mirror needs."""

    def __init__(self, gms_url: str, token: str | None) -> None:
        self._gms_url = gms_url.rstrip("/")
        self._token = token

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._gms_url}/api/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            # This GMS was measured returning GraphQL errors as HTTP 200 with
            # an `errors` array, but a proxy or another release may use the
            # status line instead. Re-raise with the BODY, not only the code:
            # the body carries both the propagation marker the retry matches
            # on and the reason the CLI's honest report needs to print.
            body = error.read().decode("utf-8", "replace")[:1000]
            raise RuntimeError(f"HTTP {error.code} from GraphQL: {body}") from error
        if not isinstance(payload, dict):
            raise TypeError("DataHub GraphQL response was not an object")
        errors = payload.get("errors")
        if errors:
            raise RuntimeError(errors)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TypeError("DataHub GraphQL response had no data object")
        return data

    def get_aspect_json(self, urn: str, aspect: str) -> dict[str, Any] | None:
        headers = {"X-RestLi-Protocol-Version": "2.0.0"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        encoded_urn = urllib.parse.quote(urn, safe="")
        query = urllib.parse.urlencode({"aspect": aspect, "version": 0})
        request = urllib.request.Request(
            f"{self._gms_url}/aspects/{encoded_urn}?{query}", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        if not isinstance(payload, dict):
            raise TypeError("DataHub aspect response was not an object")
        return payload


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
    transport: Any | None = None,
    *,
    gms_url: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """Emit native assertion definitions and one run event per receipt rule.

    This is intentionally separate from receipt MCP writeback and callers must
    opt in. Definitions and events use the documented GraphQL surface because
    the official MCP server does not expose an assertion mutation tool.
    """

    if not receipts:
        # Every receipt write can be rejected, and then there is nothing to
        # mirror. Opening a transport anyway would turn a reported writeback
        # failure into a second, unrelated connection error.
        return {
            "created": (),
            "existing": (),
            "runs": (),
            "retired": (),
            "skipped": (),
        }

    if transport is None:
        # DATAHUB_GMS_URL and nothing else, because that is the variable the
        # MCP server process reads to reach the catalog the receipts were just
        # written to. Defaulting to localhost here once meant a run against a
        # remote catalog could write its receipts there and its assertions into
        # whatever happened to be listening locally — two catalogs, one report
        # of success. An unset variable is refused rather than guessed.
        target = gms_url or os.environ.get("DATAHUB_GMS_URL", "")
        if not target:
            raise AssertionMirrorUnavailable(_MIRROR_CONFIG_REQUIRED)
        transport = _GmsTransport(target, os.environ.get("DATAHUB_GMS_TOKEN"))

    created: list[str] = []
    existing: list[str] = []
    runs: list[str] = []
    retired: list[str] = []
    skipped: list[str] = []

    # Accumulated across the whole call, not per receipt: two receipts for one
    # dataset would otherwise each retire the other's rules.
    emitted: dict[str, set[str]] = {}
    last: dict[str, Receipt] = {}
    for receipt in receipts:
        seen = emitted.setdefault(receipt.urn, set())
        last[receipt.urn] = receipt
        for rule_id, severity, summary, logic in _rule_reports(receipt):
            urn = assertion_urn(receipt.urn, rule_id)
            current = transport.get_aspect_json(urn, "assertionInfo")
            if _is_removed(transport, urn):
                # The operator deleted this row. Writing it again would
                # overrule that with a metadata write they never asked for, so
                # the run reports the skip instead of quietly reversing the
                # decision.
                skipped.append(urn)
                seen.add(urn)
                continue

            # Emitted every run, not only the first. The logic string carries
            # this run's evidence summary, so skipping the write when the
            # assertion already exists would leave the catalog displaying the
            # first run's reasoning forever while the run events beneath it
            # said something else.
            _upsert_definition(
                transport,
                urn,
                receipt.urn,
                _assertion_name(rule_id, severity),
                logic,
            )
            (existing if current is not None else created).append(urn)

            runs.append(
                _report_run(
                    transport,
                    urn,
                    receipt,
                    _native_results(receipt, rule_id, severity, summary),
                    assertion_result_type(receipt.verdict)
                    if not severity
                    else assertion_result_for_severity(severity),
                    evidence_url=receipt.evidence_url,
                    freshly_created=current is None,
                )
            )
            seen.add(urn)

    # A rule Sidq no longer reports must stop claiming a failure. The catalog
    # keeps every assertion ever written, so without this a fixed problem stays
    # red beside the run that fixed it, and the surface this feature exists to
    # serve states something Sidq no longer holds. Retiring means one more
    # honest run event, not a deletion.
    for dataset_urn, seen in emitted.items():
        receipt = last[dataset_urn]
        for stale, stale_rule in _ours_not_emitted(transport, dataset_urn, seen):
            # The definition is rewritten too, so the row stops displaying the
            # last firing's reasoning and any "(warning)" in its name, and so
            # the next run can see it is already retired instead of closing it
            # again every run for the life of the dataset.
            _upsert_definition(
                transport,
                stale,
                dataset_urn,
                f"{_NAME_PREFIX}{stale_rule}",
                _RETIRED_SUMMARY,
            )
            runs.append(
                _report_run(
                    transport,
                    stale,
                    receipt,
                    _native_results(receipt, stale_rule, "retired", _RETIRED_SUMMARY),
                    "SUCCESS",
                )
            )
            retired.append(stale)

    return {
        "created": tuple(created),
        "existing": tuple(existing),
        "runs": tuple(runs),
        "retired": tuple(retired),
        "skipped": tuple(skipped),
    }


def _upsert_definition(
    transport: Any, urn: str, dataset_urn: str, name: str, logic: str
) -> None:
    data = transport.graphql(
        _UPSERT_CUSTOM_ASSERTION,
        {
            "u": urn,
            "i": {
                "entityUrn": dataset_urn,
                "type": _CUSTOM_TYPE,
                # DataHub renders description as the assertion name in its
                # list, so this is the short rule identity a person sees.
                "description": name,
                "platform": {"name": "sidq"},
                "logic": logic,
            },
        },
    )
    stored = (data.get("upsertCustomAssertion") or {}).get("urn")
    if stored and stored != urn:
        # A server-minted urn would silently desync every subsequent report
        # and retirement lookup, so a mismatch is an error, not a curiosity.
        raise RuntimeError(f"catalog stored assertion urn {stored}, expected {urn}")


# The report resolver resolves the assertion's entity association through a
# path that lags a just-written definition: measured live, a report issued
# immediately after a successful upsert returns HTTP 500 "does not exist or is
# not associated with any entity", and the same report succeeds seconds later.
# The SQL-backed aspect read is already visible at that point, so polling it
# proves nothing; the only honest readiness signal is the resolver itself.
_PROPAGATION_MARKER = "does not exist or is not associated"
# 10 attempts with 2s between them: at most ~20s of waiting per freshly
# created assertion, on top of the HTTP calls themselves. The bound is stated
# in RECEIPT-SPEC; a permanent mis-association carrying the same message burns
# this budget once and then surfaces unchanged.
_PROPAGATION_ATTEMPTS = 10
_PROPAGATION_WAIT_SECONDS = 2.0


def _report_run(
    transport: Any,
    urn: str,
    receipt: Receipt,
    results: dict[str, str],
    result_type: str,
    *,
    evidence_url: str = "",
    freshly_created: bool = False,
) -> str:
    timestamp = _checked_at_millis(receipt.checked_at)
    result: dict[str, Any] = {
        "type": result_type,
        "timestampMillis": timestamp,
        "properties": [{"key": key, "value": value} for key, value in results.items()],
    }
    if evidence_url:
        result["externalUrl"] = evidence_url
    attempts = _PROPAGATION_ATTEMPTS if freshly_created else 1
    for attempt in range(attempts):
        try:
            transport.graphql(_REPORT_ASSERTION_RESULT, {"u": urn, "r": result})
            break
        except RuntimeError as error:
            # Retry ONLY the measured propagation failure, and only for a
            # definition this very call created — anything else is a real
            # error and is raised on the spot, unchanged.
            if _PROPAGATION_MARKER not in str(error) or attempt == attempts - 1:
                raise
            time.sleep(_PROPAGATION_WAIT_SECONDS)
    # DataHub derives runId from timestampMillis. Reports retried with the
    # same receipt timestamp replace that event, providing idempotency without
    # the SDK's old content-hash messageId.
    return f"{urn}@{timestamp}"


def _is_removed(transport: Any, urn: str) -> bool:
    # The Rest.li read wraps the aspect: {"version":0,"aspect":
    # {"com.linkedin.common.Status":{"removed":true}}}. Reading `removed` off
    # the top level returns None for every assertion, which would silently turn
    # soft-delete respect off — measured against the live catalog before this
    # unwrap was written.
    payload = transport.get_aspect_json(urn, "status")
    if not payload:
        return False
    aspect = payload.get("aspect")
    if not isinstance(aspect, dict):
        return False
    status = aspect.get("com.linkedin.common.Status")
    return bool(isinstance(status, dict) and status.get("removed"))


def _ours_not_emitted(
    transport: Any, dataset_urn: str, emitted: set[str]
) -> Iterable[tuple[str, str]]:
    """Yield ``(assertion_urn, rule_id)`` for Sidq rules absent from this run.

    The index-backed GraphQL query brings every candidate's description,
    custom type, and logic in one call; the SDK path needed two aspect reads
    per assertion. It can lag, as the old relationship query could, which is
    acceptable for retirement rather than a new run's critical write path.

    A URN Sidq did not mint cannot be one of ours. A custom assertion with a
    different type is someone else's statement. A soft-deleted assertion stays
    deleted, and an already-retired assertion gets no further closing events.
    """

    # Paged to exhaustion rather than capped: the SDK path's relationship
    # reader paginated, and a silent cap would read as "nothing left to
    # retire" on a dataset carrying more assertions than one page.
    assertions: list[Any] = []
    start = 0
    while True:
        data = transport.graphql(
            _DATASET_ASSERTIONS,
            {"u": dataset_urn, "start": start, "count": _ASSERTIONS_PAGE},
        )
        dataset = data.get("dataset")
        if not isinstance(dataset, dict):
            return
        assertion_page = dataset.get("assertions")
        if not isinstance(assertion_page, dict):
            return
        page = assertion_page.get("assertions")
        if not isinstance(page, list):
            return
        assertions.extend(page)
        total = assertion_page.get("total")
        start += len(page)
        if not page or not isinstance(total, int) or start >= total:
            break

    for item in assertions:
        if not isinstance(item, dict):
            continue
        urn = item.get("urn")
        if (
            not isinstance(urn, str)
            or urn in emitted
            or not urn.startswith(_URN_PREFIX)
        ):
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        custom = info.get("customAssertion")
        if not isinstance(custom, dict) or custom.get("type") != _CUSTOM_TYPE:
            continue
        if custom.get("logic") == _RETIRED_SUMMARY:
            continue
        description = info.get("description")
        if not isinstance(description, str) or not description.startswith(_NAME_PREFIX):
            continue
        if _is_removed(transport, urn):
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


def _checked_at_millis(checked_at: str) -> int:
    timestamp = datetime.fromisoformat(checked_at)
    return int(timestamp.astimezone(UTC).timestamp() * 1000)
