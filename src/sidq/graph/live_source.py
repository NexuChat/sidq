"""Read-only live-schema adapter for the reality gate."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from sidq.graph.client import DatasetInfo, SchemaField


@runtime_checkable
class LiveSourceClient(Protocol):
    def get_dataset(self, urn: str) -> DatasetInfo | None: ...


ConnectionFactory = Callable[[], Any]


class PostgresLiveSourceClient:
    """A small DB-API reader over ``information_schema.columns`` only."""

    def __init__(self, connection_or_factory: Any | ConnectionFactory) -> None:
        self._connection_or_factory = connection_or_factory

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        relation = _relation_from_urn(urn)
        if relation is None:
            return None
        schema, table = relation
        connection = self._connection_or_factory() if callable(self._connection_or_factory) else self._connection_or_factory
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema, table),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
        if not rows:
            return None
        return DatasetInfo(
            urn=urn,
            fields=tuple(SchemaField(str(name), str(native_type), str(nullable).upper() == "YES") for name, native_type, nullable in rows),
        )


def _relation_from_urn(urn: str) -> tuple[str, str] | None:
    """Extract the final ``schema.table`` components of a DataHub dataset URN."""
    match = re.fullmatch(r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)", urn)
    if match is None:
        return None
    parts = [part.strip('`"[] ') for part in match.group(1).split(".") if part]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None
