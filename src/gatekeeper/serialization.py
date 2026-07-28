"""Canonical, byte-stable JSON serialization for sidq artifacts."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any


def _sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_data(value: Any) -> Any:
    """Convert supported values to a recursively sorted JSON-compatible tree."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_data(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): canonical_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [canonical_data(item) for item in value]
        return sorted(items, key=_sort_key)
    return value


def canonical_json(value: Any) -> bytes:
    """Return UTF-8 canonical JSON suitable for hashing and external artifacts."""
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def serialize(value: Any) -> bytes:
    """Compatibility name for the sole JSON serialization surface."""
    return canonical_json(value)
