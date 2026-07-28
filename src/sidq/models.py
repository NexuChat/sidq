"""Immutable vocabulary shared by the Sidq engine."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FieldRef:
    dataset_urn: str
    field_path: str


@dataclass(frozen=True, slots=True)
class TouchedAsset:
    urn: str
    source_path: str
    added_fields: tuple[str, ...]
    removed_fields: tuple[str, ...]
    referenced_fields: tuple[FieldRef, ...]
    resolution_strategy: str = ""


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    subject: str
    detail: dict
    graph_links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            json.dumps(self.detail)
        except (TypeError, ValueError) as error:
            raise TypeError("Evidence.detail must be JSON-serializable") from error


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    severity: str
    message: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: str
    reason_code: str | None
    findings: tuple[Finding, ...]
    touched: tuple[TouchedAsset, ...]
    commit_sha: str
    policy_hash: str
