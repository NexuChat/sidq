"""Read a receipt back from DataHub and compute whether it is still trustworthy."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

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
        from .write import StdioMCPReceiptToolCaller

        tool_caller = StdioMCPReceiptToolCaller()
    try:
        entity = _single_entity(tool_caller("get_entities", {"urns": [urn]}), urn)
    finally:
        if owns_caller:
            close = getattr(tool_caller, "close", None)
            if callable(close):
                close()
    values = _sidq_values(entity)
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
    }
    stale, reason = _staleness(
        checked_at=result["checked_at"],
        schema_modified_at=schema_modified_at or _schema_modified_at(entity),
        recorded_policy_hash=result["policy_hash"],
        current_policy_hash=current_policy_hash,
        max_age=max_age,
        now=now or datetime.now(UTC),
    )
    result["stale"] = stale
    result["stale_reason"] = reason
    return result


def _single_entity(raw: Any, requested_urn: str) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        nested = raw.get("entities") or raw.get("result")
        if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            raw = nested
        elif raw.get("urn") == requested_urn or "structuredProperties" in raw:
            return raw
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if isinstance(item, Mapping) and item.get("urn") == requested_urn:
                return item
        for item in raw:
            if isinstance(item, Mapping):
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


def _schema_modified_at(entity: Mapping[str, Any]) -> datetime | None:
    for name in ("schemaMetadata", "editableSchemaMetadata"):
        aspect = entity.get(name)
        if isinstance(aspect, Mapping):
            timestamp = _timestamp(aspect.get("lastModified"))
            if timestamp is not None:
                return timestamp
    return None


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
    recorded_policy_hash: str | None,
    current_policy_hash: str | None,
    max_age: timedelta,
    now: datetime,
) -> tuple[bool, str | None]:
    checked = _parse_iso(checked_at)
    if checked is None:
        return True, "receipt has no valid checked_at timestamp"
    if schema_modified_at is not None and schema_modified_at > checked:
        return True, "asset schema changed after the last verification"
    if now.astimezone(UTC) - checked > max_age:
        return True, "receipt exceeded the maximum verification age"
    if current_policy_hash is not None and current_policy_hash != recorded_policy_hash:
        return True, "policy hash changed since the last verification"
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
    if status.get("stale"):
        return False, f"receipt is stale: {status.get('stale_reason') or 'unknown'}"
    if verdict == "BLOCK":
        return (
            False,
            f"receipt records a refusal ({status.get('reason_code') or verdict})",
        )
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
