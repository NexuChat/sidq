"""Read-only live-schema adapter for the reality gate."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from sidq.graph.client import DatasetInfo, SchemaField


@runtime_checkable
class LiveSourceClient(Protocol):
    def get_dataset(self, urn: str) -> DatasetInfo | None: ...


ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class LiveConstraint:
    """One constraint reported by the live PostgreSQL catalog.

    ``definition`` is deliberately the text PostgreSQL reports through
    ``pg_get_constraintdef`` (or its equivalent for ``NOT NULL``).  Consumers
    should retain it verbatim in their evidence instead of reconstructing SQL.
    """

    name: str
    kind: str
    columns: tuple[str, ...]
    definition: str
    target_schema: str | None = None
    target_table: str | None = None
    target_columns: tuple[str, ...] = ()
    predicate: str | None = None
    is_partial: bool = False


class PostgresLiveSourceClient:
    """A small read-only DB-API reader over PostgreSQL schema metadata."""

    def __init__(self, connection_or_factory: Any | ConnectionFactory) -> None:
        self._connection_or_factory = connection_or_factory

    def get_dataset(self, urn: str) -> DatasetInfo | None:
        relation = _relation_from_urn(urn)
        if relation is None:
            return None
        schema, table = relation
        connection = (
            self._connection_or_factory()
            if callable(self._connection_or_factory)
            else self._connection_or_factory
        )
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
            fields=tuple(
                SchemaField(str(name), str(native_type), str(nullable).upper() == "YES")
                for name, native_type, nullable in rows
            ),
        )

    def get_constraints(self, urn: str) -> tuple[LiveConstraint, ...]:
        """Read constraints for one relation without modifying the source.

        PostgreSQL spreads enforcement across attributes, constraints, and
        indexes, so consulting only one catalog would produce false claims
        about what the source accepts.
        """
        relation = _relation_from_urn(urn)
        if relation is None:
            return ()
        schema, table = relation
        connection = (
            self._connection_or_factory()
            if callable(self._connection_or_factory)
            else self._connection_or_factory
        )
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT a.attname, format('%%I NOT NULL', a.attname)
                FROM pg_attribute AS a
                JOIN pg_class AS relation ON relation.oid = a.attrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = %s
                  AND relation.relname = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND a.attnotnull
                ORDER BY a.attnum
                """,
                (schema, table),
            )
            not_null_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT
                    constraint_row.conname,
                    constraint_row.contype,
                    pg_get_constraintdef(constraint_row.oid, true),
                    ARRAY(
                        SELECT attribute.attname
                        FROM unnest(constraint_row.conkey) WITH ORDINALITY
                            AS key_column(attnum, ordinal_position)
                        JOIN pg_attribute AS attribute
                          ON attribute.attrelid = constraint_row.conrelid
                         AND attribute.attnum = key_column.attnum
                        ORDER BY key_column.ordinal_position
                    ),
                    target_namespace.nspname,
                    target_relation.relname,
                    ARRAY(
                        SELECT attribute.attname
                        FROM unnest(constraint_row.confkey) WITH ORDINALITY
                            AS key_column(attnum, ordinal_position)
                        JOIN pg_attribute AS attribute
                          ON attribute.attrelid = constraint_row.confrelid
                         AND attribute.attnum = key_column.attnum
                        ORDER BY key_column.ordinal_position
                    )
                FROM pg_constraint AS constraint_row
                JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                LEFT JOIN pg_class AS target_relation
                  ON target_relation.oid = constraint_row.confrelid
                LEFT JOIN pg_namespace AS target_namespace
                  ON target_namespace.oid = target_relation.relnamespace
                WHERE namespace.nspname = %s
                  AND relation.relname = %s
                ORDER BY constraint_row.contype, constraint_row.conname
                """,
                (schema, table),
            )
            constraint_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT
                    index_relation.relname,
                    pg_get_indexdef(index_row.indexrelid),
                    ARRAY(
                        SELECT attribute.attname
                        FROM unnest(index_row.indkey) WITH ORDINALITY
                            AS key_column(attnum, ordinal_position)
                        LEFT JOIN pg_attribute AS attribute
                          ON attribute.attrelid = index_row.indrelid
                         AND attribute.attnum = key_column.attnum
                        ORDER BY key_column.ordinal_position
                    ),
                    pg_get_expr(index_row.indpred, index_row.indrelid),
                    index_row.indpred IS NOT NULL
                FROM pg_index AS index_row
                JOIN pg_class AS relation ON relation.oid = index_row.indrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                JOIN pg_class AS index_relation
                  ON index_relation.oid = index_row.indexrelid
                LEFT JOIN pg_constraint AS constraint_row
                  ON constraint_row.conindid = index_row.indexrelid
                WHERE namespace.nspname = %s
                  AND relation.relname = %s
                  AND index_row.indisunique
                  AND NOT index_row.indisprimary
                  AND constraint_row.oid IS NULL
                ORDER BY index_relation.relname
                """,
                (schema, table),
            )
            index_rows = cursor.fetchall()
        finally:
            cursor.close()

        constraints = [
            LiveConstraint(
                name=f"{column}_not_null",
                kind="not_null",
                columns=(str(column),),
                definition=str(definition),
            )
            for column, definition in not_null_rows
        ]
        kind_by_code = {
            "c": "check",
            "p": "primary_key",
            "u": "unique",
            "f": "foreign_key",
            "x": "exclude",
        }
        constraints.extend(
            LiveConstraint(
                name=str(name),
                kind=kind_by_code.get(str(kind), "opaque"),
                columns=_columns(columns),
                definition=str(definition),
                target_schema=str(target_schema) if target_schema is not None else None,
                target_table=str(target_table) if target_table is not None else None,
                target_columns=_columns(target_columns),
            )
            for (
                name,
                kind,
                definition,
                columns,
                target_schema,
                target_table,
                target_columns,
            ) in constraint_rows
        )
        for name, definition, columns, predicate, is_partial in index_rows:
            index_columns, has_expression = _index_columns(columns)
            constraints.append(
                LiveConstraint(
                    name=str(name),
                    kind="opaque" if has_expression else "unique",
                    columns=index_columns,
                    definition=str(definition),
                    predicate=str(predicate) if predicate is not None else None,
                    is_partial=bool(is_partial),
                )
            )
        return tuple(sorted(constraints, key=lambda item: (item.kind, item.name)))


def _relation_from_urn(urn: str) -> tuple[str, str] | None:
    """Extract the final ``schema.table`` components of a DataHub dataset URN."""
    match = re.fullmatch(
        r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)", urn
    )
    if match is None:
        return None
    parts = [part.strip('`"[] ') for part in match.group(1).split(".") if part]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else None


def _columns(value: Any) -> tuple[str, ...]:
    """Normalise DB-API array results without guessing at unknown encodings."""
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _index_columns(value: Any) -> tuple[tuple[str, ...], bool]:
    """Omit expression keys without overstating them as column uniqueness."""
    if not isinstance(value, (list, tuple)):
        return (), False
    return (
        tuple(str(item) for item in value if item is not None),
        any(item is None for item in value),
    )
