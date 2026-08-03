"""Read a receipt back from DataHub and compute whether it is still trustworthy."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sidq.serialization import canonical_json

ToolCaller = Callable[[str, Mapping[str, Any]], Any]
_PREFIX = "urn:li:structuredProperty:sidq."


def get_verification_status(
    urn: str,
    tool_caller: ToolCaller | None = None,
    *,
    current_policy_hash: str | None = None,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
    schema_modified_at: datetime | None = None,
) -> dict[str, Any]:
    """Return the latest receipt with *computed*, never persisted, staleness.

    ``get_entities`` is intentionally called here rather than trusting a writer
    acknowledgement: consumption happens in a separate caller/process.
    """

    owns_caller = tool_caller is None
    if tool_caller is None:
        from .write import RECEIPT_READ_TOOLS, StdioMCPReceiptToolCaller

        # Consuming a receipt reads; it never writes. This transport is given
        # the read tools only, so the mutation-enabled child it starts cannot
        # be used to change anything.
        tool_caller = StdioMCPReceiptToolCaller(RECEIPT_READ_TOOLS)
    try:
        entity = _single_entity(tool_caller("get_entities", {"urns": [urn]}), urn)
        recorded_context_hash = _one(_sidq_values(entity), "context_hash")
        current_context_hash = _current_context_hash(
            urn, entity, recorded_context_hash, tool_caller
        )
    finally:
        if owns_caller:
            close = getattr(tool_caller, "close", None)
            if callable(close):
                close()
    return _status_of(
        urn,
        entity,
        current_policy_hash=current_policy_hash,
        max_age=max_age,
        now=now,
        schema_modified_at=schema_modified_at,
        current_context_hash=current_context_hash,
    )


# `get_entities` takes a list, so reading the whole catalog's receipts does not
# have to cost one call per asset. The batch size is a courtesy to the server,
# not a correctness bound; any chunking returns the same statuses.
_BATCH = 20


def get_verification_statuses(
    urns: Sequence[str],
    tool_caller: ToolCaller,
    *,
    current_policy_hash: str | None = None,
    max_age: timedelta = timedelta(days=7),
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Read many receipts at once, one bounded ``get_entities`` call per batch.

    Every requested URN gets a status. An asset the response did not mention comes
    back with no verdict — indistinguishable from "never verified", which is the
    correct reading: a receipt that cannot be produced does not vouch for anything.
    """
    statuses: dict[str, dict[str, Any]] = {}
    ordered = list(dict.fromkeys(str(urn) for urn in urns))
    for start in range(0, len(ordered), _BATCH):
        chunk = ordered[start : start + _BATCH]
        by_urn = _entities_by_urn(tool_caller("get_entities", {"urns": chunk}))
        for urn in chunk:
            entity = by_urn.get(urn, {})
            recorded_context_hash = _one(_sidq_values(entity), "context_hash")
            current_context_hash = _current_context_hash(
                urn, entity, recorded_context_hash, tool_caller
            )
            statuses[urn] = _status_of(
                urn,
                entity,
                current_policy_hash=current_policy_hash,
                max_age=max_age,
                now=now,
                current_context_hash=current_context_hash,
            )
    return statuses


def _status_of(
    urn: str,
    entity: Mapping[str, Any],
    *,
    current_policy_hash: str | None,
    max_age: timedelta,
    now: datetime | None,
    schema_modified_at: datetime | None = None,
    current_context_hash: str | None = None,
) -> dict[str, Any]:
    values = _sidq_values(entity)
    recorded_context_hash = _one(values, "context_hash")
    result: dict[str, Any] = {
        "urn": urn,
        "verdict": _one(values, "verdict"),
        "reason_code": _one(values, "reason_code") or None,
        "commit_sha": _one(values, "commit_sha"),
        "checked_at": _one(values, "checked_at"),
        "policy_hash": _one(values, "policy_hash"),
        "rules_fired": values.get("rules_fired", []),
        "verifier": _one(values, "verifier"),
        "evidence_url": _one(values, "evidence_url"),
        "context_hash": recorded_context_hash,
        # Present only on receipts a swarm wrote; a reader uses them to say
        # *which* worker vouched for an asset it skipped.
        "swarm_run": _one(values, "swarm_run"),
        "worker_id": _one(values, "worker_id"),
    }
    stale, reason = _staleness(
        checked_at=result["checked_at"],
        schema_modified_at=(
            schema_modified_at
            or (_schema_modified_at(entity) if not recorded_context_hash else None)
        ),
        recorded_context_hash=recorded_context_hash,
        current_context_hash=current_context_hash,
        recorded_policy_hash=result["policy_hash"],
        current_policy_hash=current_policy_hash,
        max_age=max_age,
        now=now or datetime.now(UTC),
    )
    result["stale"] = stale
    result["stale_reason"] = reason
    return result


def _entities_by_urn(raw: Any) -> dict[str, Mapping[str, Any]]:
    """Index a ``get_entities`` response by URN, tolerating the same shapes
    ``_single_entity`` does. An unrecognisable payload indexes nothing, and the
    caller reads that as "no receipt" rather than as an error to swallow."""
    if isinstance(raw, Mapping):
        nested = raw.get("entities") or raw.get("result")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            raw = nested
        elif isinstance(raw.get("urn"), str):
            return {str(raw["urn"]): raw}
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return {}
    return {
        str(item["urn"]): item
        for item in raw
        if isinstance(item, Mapping) and isinstance(item.get("urn"), str)
    }


def _single_entity(raw: Any, requested_urn: str) -> Mapping[str, Any]:
    """The entity that IS the requested asset — never merely the first returned.

    This used to fall back to the first entity in the response when no URN
    matched, which handed the catalog a way to answer a question it was not
    asked: return asset B's receipt when A was requested, and `verify A` prints
    VERIFIED on the strength of B's evidence. A tool built to stop agents
    trusting the graph cannot itself trust the graph to reply about the right
    asset. An unmatched response is treated as no receipt, which reads as
    NOT VERIFIED — the safe answer.
    """
    if isinstance(raw, Mapping):
        nested = raw.get("entities") or raw.get("result")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            raw = nested
        elif raw.get("urn") == requested_urn:
            return raw
        else:
            return {}
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Mapping) and item.get("urn") == requested_urn:
                return item
    return {}


def _sidq_values(entity: Mapping[str, Any]) -> dict[str, list[str]]:
    assignments = (
        entity.get("structuredProperties") or entity.get("structured_properties") or {}
    )
    if isinstance(assignments, Mapping):
        assignments = (
            assignments.get("properties")
            or assignments.get("structuredProperties")
            or []
        )
    result: dict[str, list[str]] = {}
    if not isinstance(assignments, Sequence) or isinstance(assignments, (str, bytes)):
        return result
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            continue
        property_urn = assignment.get("structuredPropertyUrn") or assignment.get(
            "propertyUrn"
        )
        structured_property = assignment.get("structuredProperty")
        if not property_urn and isinstance(structured_property, Mapping):
            property_urn = structured_property.get("urn")
        if not isinstance(property_urn, str) or not property_urn.startswith(_PREFIX):
            continue
        raw_values = assignment.get("values") or assignment.get("value") or []
        if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
            raw_values = [raw_values]
        parsed: list[str] = []
        for value in raw_values:
            if isinstance(value, Mapping):
                value = value.get(
                    "stringValue", value.get("value", value.get("numberValue"))
                )
            if value is not None:
                parsed.append(str(value))
        result[property_urn.removeprefix(_PREFIX)] = parsed
    return result


def _one(values: Mapping[str, list[str]], name: str) -> str | None:
    value = values.get(name, [])
    return value[0] if value else None


_SIDQ_BADGES = frozenset({"urn:li:tag:sidq:verified", "urn:li:tag:sidq:blocked"})
_AUDIT_KEYS = frozenset(
    {"created", "createdon", "lastmodified", "lastobserved", "timestamp"}
)


def decision_context_hash(
    urn: str, entity: Mapping[str, Any], tool_caller: ToolCaller
) -> str:
    """Hash the semantic entity plus complete immediate lineage in both directions."""
    if entity.get("urn") != urn:
        raise RuntimeError("decision context entity does not match the requested URN")
    values = _sidq_values(entity)
    context = {
        "entity": _semantic_context(
            entity,
            evidence_urls=frozenset(values.get("evidence_url", [])),
        ),
        "downstream": _lineage_context(urn, tool_caller, upstream=False),
        "upstream": _lineage_context(urn, tool_caller, upstream=True),
    }
    return f"sha256:{hashlib.sha256(canonical_json(context)).hexdigest()}"


def _current_context_hash(
    urn: str,
    entity: Mapping[str, Any],
    recorded_context_hash: str | None,
    tool_caller: ToolCaller,
) -> str | None:
    if not recorded_context_hash:
        return None
    try:
        return decision_context_hash(urn, entity, tool_caller)
    except Exception:  # noqa: BLE001 - an unprovable read is stale, never fatal
        return None


def _lineage_context(
    urn: str, tool_caller: ToolCaller, *, upstream: bool
) -> Mapping[str, Any]:
    raw = tool_caller(
        "get_lineage",
        {
            "urn": urn,
            "upstream": upstream,
            "max_hops": 1,
            "max_results": 100,
        },
    )
    if not isinstance(raw, Mapping):
        raise TypeError("decision context lineage response must be an object")
    direction = "upstreams" if upstream else "downstreams"
    section = raw.get(direction)
    if not isinstance(section, Mapping):
        raise TypeError(f"decision context lineage is missing {direction}")
    if _is_official_empty_lineage(section):
        return {"total": 0, "results": []}
    results = section.get("searchResults")
    total = section.get("total")
    returned = section.get("returned")
    has_more = section.get("hasMore", section.get("has_more"))
    if (
        not isinstance(results, list)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(returned, int)
        or isinstance(returned, bool)
        or total != returned
        or returned != len(results)
        or has_more is not False
    ):
        raise RuntimeError(f"decision context {direction} lineage is incomplete")
    return {"total": total, "results": _semantic_context(results)}


def _is_official_empty_lineage(value: Mapping[str, Any]) -> bool:
    total = value.get("total")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and total == 0
        and all(
            key not in value
            for key in ("searchResults", "returned", "hasMore", "has_more")
        )
    )


def _semantic_context(
    value: Any,
    *,
    evidence_urls: frozenset[str] = frozenset(),
    managed_surfaces: bool = True,
) -> Any:
    if isinstance(value, Mapping):
        semantic: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in _AUDIT_KEYS:
                continue
            if managed_surfaces and name in {
                "structuredProperties",
                "structured_properties",
            }:
                semantic[name] = _without_sidq_properties(
                    item, evidence_urls=evidence_urls
                )
                continue
            if managed_surfaces and name in {"tags", "globalTags", "global_tags"}:
                normalized_tags = _semantic_tag_context(
                    item, evidence_urls=evidence_urls
                )
                if normalized_tags not in ({}, []):
                    semantic[name] = normalized_tags
                continue
            if managed_surfaces and name == "relatedDocuments":
                semantic[name] = _without_sidq_receipt_documents(
                    item, evidence_urls=evidence_urls
                )
                continue
            semantic[name] = _semantic_context(
                item, evidence_urls=evidence_urls, managed_surfaces=False
            )
        return semantic
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _semantic_context(item, evidence_urls=evidence_urls, managed_surfaces=False)
            for item in value
        ]
    return value


def _semantic_tag_context(value: Any, *, evidence_urls: frozenset[str]) -> Any:
    if _is_sidq_tag_assignment(value):
        return {}
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = (
                _semantic_tag_context(item, evidence_urls=evidence_urls)
                if str(key) == "tags"
                else _semantic_metadata_context(item)
            )
            if cleaned not in ({}, []):
                normalized[str(key)] = cleaned
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        normalized_items = [
            _semantic_tag_context(item, evidence_urls=evidence_urls) for item in value
        ]
        return [item for item in normalized_items if item not in ({}, [])]
    return _semantic_context(value, evidence_urls=evidence_urls, managed_surfaces=False)


def _semantic_metadata_context(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_metadata_context(item)
            for key, item in value.items()
            if str(key).casefold() not in _AUDIT_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_semantic_metadata_context(item) for item in value]
    return value


def _is_sidq_tag_assignment(value: Any) -> bool:
    if isinstance(value, str):
        return value in _SIDQ_BADGES
    if not isinstance(value, Mapping):
        return False
    structural_keys = {"tagUrn", "urn", "tag"}
    if "tags" in value and any(key in value for key in structural_keys):
        return False
    tag_urns = {
        urn
        for urn in (value.get("tagUrn"), value.get("urn"))
        if isinstance(urn, str) and urn
    }
    nested = value.get("tag")
    if isinstance(nested, Mapping):
        nested_urn = nested.get("urn")
        if isinstance(nested_urn, str) and nested_urn:
            tag_urns.add(nested_urn)
    return len(tag_urns) == 1 and bool(tag_urns & _SIDQ_BADGES)


def _without_sidq_properties(value: Any, *, evidence_urls: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                [
                    _semantic_context(
                        item, evidence_urls=evidence_urls, managed_surfaces=False
                    )
                    for item in items
                    if not _is_sidq_property(item)
                ]
                if str(key) in {"properties", "structuredProperties"}
                and isinstance(items := item, list)
                else _without_sidq_properties(item, evidence_urls=evidence_urls)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _semantic_context(item, evidence_urls=evidence_urls, managed_surfaces=False)
            for item in value
            if not _is_sidq_property(item)
        ]
    return value


def _without_sidq_receipt_documents(
    value: Any, *, evidence_urls: frozenset[str]
) -> Any:
    if isinstance(value, Mapping):
        documents = value.get("documents")
        if isinstance(documents, Sequence) and not isinstance(documents, (str, bytes)):
            return {
                "documents": [
                    _semantic_context(
                        document, evidence_urls=evidence_urls, managed_surfaces=False
                    )
                    for document in documents
                    if not _is_sidq_receipt_document(document, evidence_urls)
                ]
            }
        return {
            str(key): _without_sidq_receipt_documents(item, evidence_urls=evidence_urls)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _semantic_context(
                document, evidence_urls=evidence_urls, managed_surfaces=False
            )
            for document in value
            if not _is_sidq_receipt_document(document, evidence_urls)
        ]
    return value


def _is_sidq_receipt_document(value: Any, evidence_urls: frozenset[str]) -> bool:
    if evidence_urls and _contains_urn(value, evidence_urls):
        return True
    if not isinstance(value, Mapping):
        return False
    info = value.get("info")
    title = info.get("title") if isinstance(info, Mapping) else None
    return (
        isinstance(title, str)
        and title.startswith("Sidq ")
        and " receipt for urn:li:" in title
    )


def _is_sidq_property(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    property_urn = value.get("structuredPropertyUrn") or value.get("propertyUrn")
    nested = value.get("structuredProperty")
    if not property_urn and isinstance(nested, Mapping):
        property_urn = nested.get("urn")
    return isinstance(property_urn, str) and property_urn.startswith(_PREFIX)


def _contains_urn(value: Any, urns: set[str] | frozenset[str]) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_urn(item, urns) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_urn(item, urns) for item in value)
    return isinstance(value, str) and value in urns


def _schema_modified_at(entity: Mapping[str, Any]) -> datetime | None:
    modified = [
        timestamp
        for name in ("schemaMetadata", "editableSchemaMetadata")
        if isinstance((aspect := entity.get(name)), Mapping)
        if (timestamp := _timestamp(aspect.get("lastModified"))) is not None
    ]
    return max(modified, default=None)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, Mapping):
        value = value.get("time")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str):
        return _parse_iso(value)
    return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (
        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _staleness(
    *,
    checked_at: str | None,
    schema_modified_at: datetime | None,
    recorded_context_hash: str | None,
    current_context_hash: str | None,
    recorded_policy_hash: str | None,
    current_policy_hash: str | None,
    max_age: timedelta,
    now: datetime,
) -> tuple[bool, str | None]:
    checked = _parse_iso(checked_at)
    if checked is None:
        return True, "receipt has no valid checked_at timestamp"
    if current_policy_hash is not None and current_policy_hash != recorded_policy_hash:
        return True, "policy hash changed since the last verification"
    if schema_modified_at is not None and schema_modified_at > checked:
        return True, "asset schema changed after the last verification"
    if now.astimezone(UTC) - checked > max_age:
        return True, "receipt exceeded the maximum verification age"
    if not recorded_context_hash:
        return True, "receipt has no decision context hash"
    if current_context_hash is None:
        return True, "asset decision context could not be proved"
    if current_context_hash != recorded_context_hash:
        return True, "asset decision context changed"
    return False, None


def holds(status: Mapping[str, Any]) -> tuple[bool, str]:
    """Decide whether a receipt read back from DataHub still vouches for an asset.

    Three separate things can be true of a receipt, and only one of them is
    "verified": it can be absent, it can be present but no longer applicable, or it
    can be present and record a refusal. Collapsing those into a boolean would let
    "we never checked" read the same as "we checked and it passed", which is the
    exact failure this project exists to prevent — so the reason travels with the
    answer and every caller has to render it.
    """
    verdict = status.get("verdict")
    if not verdict:
        return False, "no receipt on this asset"
    if verdict not in {"PASS", "WARN", "BLOCK"}:
        # The engine emits exactly three verdicts. Anything else was not written
        # by this engine, so it vouches for nothing — a catalog cannot invent a
        # fourth verdict and have it read as approval.
        return False, f"receipt records an unrecognised verdict ({verdict})"
    if verdict == "BLOCK":
        return (
            False,
            f"receipt records a refusal ({status.get('reason_code') or verdict})",
        )
    if status.get("stale"):
        return False, f"receipt is stale: {status.get('stale_reason') or 'unknown'}"
    return True, f"receipt records {verdict}"


def render_verification(urn: str, status: Mapping[str, Any]) -> list[str]:
    """Human-readable readback, showing the provenance the verdict rests on."""
    verified, reason = holds(status)
    lines = [
        f"{'VERIFIED' if verified else 'NOT VERIFIED'}  {urn}",
        f"  {reason}",
    ]
    for label, key in (
        ("checked at", "checked_at"),
        ("commit", "commit_sha"),
        ("policy", "policy_hash"),
        ("verifier", "verifier"),
        ("evidence", "evidence_url"),
    ):
        value = status.get(key)
        if value:
            lines.append(f"  {label:<10}  {value}")
    rules = status.get("rules_fired") or []
    if rules:
        lines.append(f"  {'rules':<10}  {', '.join(rules)}")
    return lines
