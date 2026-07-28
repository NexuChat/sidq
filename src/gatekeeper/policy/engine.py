"""A deliberately small, validated policy matcher with no dynamic evaluation."""

from __future__ import annotations

import hashlib
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from sidq.models import Evidence, Finding, TouchedAsset, Verdict

_OPERATORS = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "empty", "not_empty"}
)
_VALUELESS_OPERATORS = frozenset({"empty", "not_empty"})
_SEVERITIES = frozenset({"block", "warn"})


class PolicyConfigError(ValueError):
    """Raised when a policy cannot be safely interpreted."""


@dataclass(frozen=True, slots=True)
class Condition:
    field: str
    op: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    evidence_kind: str
    severity: Literal["block", "warn"]
    reason_code: str | None
    message: str
    where: tuple[Condition, ...]
    where_any: tuple[Condition, ...]


@dataclass(frozen=True, slots=True)
class Policy:
    settings: Mapping[str, Any]
    rules: tuple[Rule, ...]
    policy_hash: str


class _DetailView(dict[str, Any]):
    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as error:
            raise AttributeError(key) from error
        return _DetailView(value) if isinstance(value, dict) else value


def default_policy_path() -> Path:
    return Path(__file__).with_name("default_policy.yaml")


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyConfigError(f"{location} must be a mapping")
    return value


def _validate_field(field: Any, location: str) -> str:
    if not isinstance(field, str):
        raise PolicyConfigError(f"{location}.field must be a string")
    if (
        field in {"subject", "graph_links"}
        or field.startswith("detail.")
        and len(field) > len("detail.")
    ):
        return field
    raise PolicyConfigError(f"{location}.field is unknown: {field!r}")


def _resolve_setting(value: Any, settings: Mapping[str, Any], location: str) -> Any:
    if not isinstance(value, str) or not value.startswith("$settings."):
        return value
    setting = value.removeprefix("$settings.")
    if not setting or setting not in settings:
        raise PolicyConfigError(f"{location} refers to unknown setting: {value!r}")
    return settings[setting]


def _parse_condition(raw: Any, settings: Mapping[str, Any], location: str) -> Condition:
    condition = _require_mapping(raw, location)
    if set(condition) - {"field", "op", "value"}:
        raise PolicyConfigError(f"{location} has unsupported keys")
    field = _validate_field(condition.get("field"), location)
    op = condition.get("op")
    if op not in _OPERATORS:
        raise PolicyConfigError(f"{location}.op is unsupported: {op!r}")
    has_value = "value" in condition
    if op in _VALUELESS_OPERATORS:
        if has_value:
            raise PolicyConfigError(f"{location}.value is not valid for {op}")
        return Condition(field=field, op=op)
    if not has_value:
        raise PolicyConfigError(f"{location}.value is required for {op}")
    return Condition(
        field=field,
        op=op,
        value=_resolve_setting(condition["value"], settings, location),
    )


def _validate_message(message: Any, location: str) -> str:
    if not isinstance(message, str):
        raise PolicyConfigError(f"{location}.message must be a string")
    for _, field_name, _, _ in string.Formatter().parse(message):
        if (
            field_name
            and field_name != "subject"
            and not field_name.startswith("detail.")
        ):
            raise PolicyConfigError(
                f"{location}.message uses unknown field: {field_name!r}"
            )
    return message


def load_policy(path: str | Path | None = None) -> Policy:
    """Load and fully validate a policy before it is ever used for a decision."""
    policy_path = Path(path) if path is not None else default_policy_path()
    raw_bytes = policy_path.read_bytes()
    try:
        document = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as error:
        raise PolicyConfigError(f"invalid YAML in {policy_path}") from error
    document = _require_mapping(document, "policy")
    if document.get("version") != 1:
        raise PolicyConfigError("policy.version must be 1")
    if set(document) - {"version", "settings", "rules"}:
        raise PolicyConfigError("policy has unsupported top-level keys")
    settings = _require_mapping(document.get("settings", {}), "policy.settings")
    rules_raw = document.get("rules")
    if not isinstance(rules_raw, list):
        raise PolicyConfigError("policy.rules must be a list")
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(rules_raw):
        location = f"policy.rules[{index}]"
        rule = _require_mapping(raw_rule, location)
        if set(rule) - {"id", "match", "severity", "reason_code", "message"}:
            raise PolicyConfigError(f"{location} has unsupported keys")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_ids:
            raise PolicyConfigError(f"{location}.id must be unique and non-empty")
        seen_ids.add(rule_id)
        match = _require_mapping(rule.get("match"), f"{location}.match")
        if set(match) - {"evidence_kind", "where", "where_any"}:
            raise PolicyConfigError(f"{location}.match has unsupported keys")
        evidence_kind = match.get("evidence_kind")
        if not isinstance(evidence_kind, str) or not evidence_kind:
            raise PolicyConfigError(
                f"{location}.match.evidence_kind must be a non-empty string"
            )
        severity = rule.get("severity")
        if severity not in _SEVERITIES:
            raise PolicyConfigError(f"{location}.severity must be block or warn")
        reason_code = rule.get("reason_code")
        if reason_code is not None and not isinstance(reason_code, str):
            raise PolicyConfigError(f"{location}.reason_code must be a string")
        where_raw = match.get("where", [])
        where_any_raw = match.get("where_any", [])
        if not isinstance(where_raw, list) or not isinstance(where_any_raw, list):
            raise PolicyConfigError(f"{location}.match conditions must be lists")
        rules.append(
            Rule(
                id=rule_id,
                evidence_kind=evidence_kind,
                severity=severity,
                reason_code=reason_code,
                message=_validate_message(rule.get("message"), location),
                where=tuple(
                    _parse_condition(
                        item, settings, f"{location}.match.where[{item_index}]"
                    )
                    for item_index, item in enumerate(where_raw)
                ),
                where_any=tuple(
                    _parse_condition(
                        item, settings, f"{location}.match.where_any[{item_index}]"
                    )
                    for item_index, item in enumerate(where_any_raw)
                ),
            )
        )
    return Policy(
        settings=dict(settings),
        rules=tuple(rules),
        policy_hash=hashlib.sha256(raw_bytes).hexdigest(),
    )


def _field_value(evidence: Evidence, field: str) -> Any:
    if field == "subject":
        return evidence.subject
    if field == "graph_links":
        return evidence.graph_links
    value: Any = evidence.detail
    for part in field.removeprefix("detail.").split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _matches_condition(evidence: Evidence, condition: Condition) -> bool:
    left = _field_value(evidence, condition.field)
    right = condition.value
    try:
        match condition.op:
            case "eq":
                return left == right
            case "ne":
                return left != right
            case "gt":
                return left > right
            case "gte":
                return left >= right
            case "lt":
                return left < right
            case "lte":
                return left <= right
            case "in":
                return left in right
            case "contains":
                return right in left
            case "empty":
                return left is None or len(left) == 0
            case "not_empty":
                return left is not None and len(left) > 0
    except (TypeError, KeyError):
        return False
    raise AssertionError(f"validated operator was not implemented: {condition.op}")


def _matches_rule(evidence: Evidence, rule: Rule) -> bool:
    return (
        evidence.kind == rule.evidence_kind
        and all(_matches_condition(evidence, condition) for condition in rule.where)
        and (
            not rule.where_any
            or any(
                _matches_condition(evidence, condition) for condition in rule.where_any
            )
        )
    )


def _render(rule: Rule, evidence: Evidence) -> str:
    try:
        return rule.message.format(
            subject=evidence.subject, detail=_DetailView(evidence.detail)
        )
    except (AttributeError, KeyError, IndexError):
        return rule.message


class PolicyEngine:
    def __init__(self, policy: Policy | str | Path | None = None) -> None:
        self.policy = (
            load_policy(policy)
            if policy is None or isinstance(policy, (str, Path))
            else policy
        )

    def decide(
        self,
        evidence: Sequence[Evidence],
        *,
        touched: Sequence[TouchedAsset] = (),
        commit_sha: str = "",
    ) -> Verdict:
        findings: list[Finding] = []
        matched_rule_ids: set[tuple[int, int]] = set()
        reason_code: str | None = None
        has_block = False
        has_warn = False
        for evidence_index, item in enumerate(evidence):
            found_match = False
            for rule_index, rule in enumerate(self.policy.rules):
                if not _matches_rule(item, rule):
                    continue
                found_match = True
                matched_rule_ids.add((evidence_index, rule_index))
                findings.append(
                    Finding(rule.id, rule.severity, _render(rule, item), (item,))
                )
                if rule.severity == "block":
                    has_block = True
                    if reason_code is None and rule.reason_code is not None:
                        reason_code = rule.reason_code
                else:
                    has_warn = True
            if not found_match:
                findings.append(
                    Finding(
                        "informational",
                        "info",
                        f"No policy rule matched evidence kind {item.kind}.",
                        (item,),
                    )
                )
        decision = "BLOCK" if has_block else "WARN" if has_warn else "PASS"
        return Verdict(
            decision=decision,
            reason_code=reason_code,
            findings=tuple(findings),
            touched=tuple(touched),
            commit_sha=commit_sha,
            policy_hash=self.policy.policy_hash,
        )
