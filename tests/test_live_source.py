from __future__ import annotations

import pytest

from sidq.graph.live_source import (
    LiveConstraint,
    PostgresLiveSourceClient,
    _relation_from_urn,
)

URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,warehouse.analytics.orders,PROD)"


class _Cursor:
    def __init__(
        self, rows: list[tuple[str, str, str]], error: Exception | None = None
    ) -> None:
        self.rows = rows
        self.error = error
        self.closed = False
        self.arguments: tuple[str, str] | None = None

    def execute(self, sql: str, arguments: tuple[str, str]) -> None:
        self.arguments = arguments
        if self.error is not None:
            raise self.error

    def fetchall(self) -> list[tuple[str, str, str]]:
        return self.rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


class _ConstraintCursor:
    def __init__(
        self,
        *,
        not_null_rows: list[tuple[object, ...]] | None = None,
        constraint_rows: list[tuple[object, ...]] | None = None,
        index_rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.not_null_rows = not_null_rows or []
        self.constraint_rows = constraint_rows or []
        self.index_rows = index_rows or []
        self.current_rows: list[tuple[object, ...]] = []
        self.executed_sql: list[str] = []
        self.closed = False

    def execute(self, sql: str, arguments: tuple[str, str]) -> None:
        assert arguments == ("analytics", "orders")
        self.executed_sql.append(sql)
        if "FROM pg_attribute AS a" in sql:
            self.current_rows = self.not_null_rows
        elif "FROM pg_constraint AS constraint_row" in sql:
            self.current_rows = self.constraint_rows
        elif "FROM pg_index AS index_row" in sql:
            self.current_rows = self.index_rows
        else:
            raise AssertionError(f"Unexpected catalog query: {sql}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.current_rows

    def close(self) -> None:
        self.closed = True


class _ConstraintConnection:
    def __init__(self, cursor: _ConstraintCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _ConstraintCursor:
        return self._cursor


def test_postgres_live_source_reads_schema_and_closes_cursor() -> None:
    cursor = _Cursor([("order_id", "integer", "NO"), ("note", "text", "YES")])
    client = PostgresLiveSourceClient(lambda: _Connection(cursor))

    dataset = client.get_dataset(URN)

    assert cursor.arguments == ("analytics", "orders")
    assert cursor.closed is True
    assert dataset is not None
    assert [(field.path, field.nullable) for field in dataset.fields] == [
        ("order_id", False),
        ("note", True),
    ]


def test_postgres_live_source_propagates_timeout_after_closing_cursor() -> None:
    cursor = _Cursor([], TimeoutError("database timed out"))
    client = PostgresLiveSourceClient(_Connection(cursor))

    with pytest.raises(TimeoutError, match="timed out"):
        client.get_dataset(URN)

    assert cursor.closed is True


def test_postgres_live_source_handles_missing_relation_or_schema() -> None:
    empty = _Cursor([])
    client = PostgresLiveSourceClient(_Connection(empty))

    assert client.get_dataset("urn:not-a-dataset") is None
    assert client.get_dataset(URN) is None
    assert empty.closed is True
    assert (
        _relation_from_urn("urn:li:dataset:(urn:li:dataPlatform:postgres,orders,PROD)")
        is None
    )


def test_unique_index_without_backing_constraint_is_reported() -> None:
    definition = (
        "CREATE UNIQUE INDEX idx_only_slug ON analytics.orders USING btree (slug)"
    )
    cursor = _ConstraintCursor(
        index_rows=[("idx_only_slug", definition, ["slug"], None, False)]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert constraints == (
        LiveConstraint(
            name="idx_only_slug",
            kind="unique",
            columns=("slug",),
            definition=definition,
        ),
    )
    assert cursor.closed is True


def test_constraint_backed_unique_index_is_reported_once() -> None:
    cursor = _ConstraintCursor(
        constraint_rows=[
            (
                "orders_slug_key",
                "u",
                "UNIQUE (slug)",
                ["slug"],
                None,
                None,
                [],
            )
        ]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert [constraint.name for constraint in constraints] == ["orders_slug_key"]
    index_sql = next(sql for sql in cursor.executed_sql if "FROM pg_index" in sql)
    assert "LEFT JOIN pg_constraint" in index_sql
    assert "constraint_row.oid IS NULL" in index_sql


def test_partial_unique_index_carries_its_predicate() -> None:
    definition = (
        "CREATE UNIQUE INDEX active_slug ON analytics.orders USING btree (slug) "
        "WHERE (deleted_at IS NULL)"
    )
    cursor = _ConstraintCursor(
        index_rows=[
            (
                "active_slug",
                definition,
                ["slug"],
                "(deleted_at IS NULL)",
                True,
            )
        ]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert constraints[0].kind == "unique"
    assert constraints[0].predicate == "(deleted_at IS NULL)"
    assert constraints[0].is_partial is True


def test_expression_unique_index_is_opaque() -> None:
    definition = (
        "CREATE UNIQUE INDEX lower_slug ON analytics.orders USING btree (lower(slug))"
    )
    cursor = _ConstraintCursor(
        index_rows=[("lower_slug", definition, [None], None, False)]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert constraints[0].kind == "opaque"
    assert constraints[0].columns == ()
    assert constraints[0].definition == definition


def test_exclude_constraint_is_reported_with_verbatim_definition() -> None:
    definition = "EXCLUDE USING gist (room WITH =, during WITH &&)"
    cursor = _ConstraintCursor(
        constraint_rows=[
            (
                "no_overlap",
                "x",
                definition,
                ["room", "during"],
                None,
                None,
                [],
            )
        ]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert constraints[0].kind == "exclude"
    assert constraints[0].definition == definition


def test_unknown_constraint_type_is_never_dropped() -> None:
    cursor = _ConstraintCursor(
        constraint_rows=[("future_kind", "z", "FUTURE RULE", [], None, None, [])]
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    assert constraints[0].kind == "opaque"
    assert constraints[0].definition == "FUTURE RULE"
    constraint_sql = next(
        sql for sql in cursor.executed_sql if "FROM pg_constraint" in sql
    )
    assert "contype IN" not in constraint_sql


def test_constraint_output_ordering_is_deterministic() -> None:
    cursor = _ConstraintCursor(
        not_null_rows=[("zeta", "zeta NOT NULL")],
        constraint_rows=[
            ("z_check", "c", "CHECK (zeta > 0)", ["zeta"], None, None, []),
            ("a_check", "c", "CHECK (alpha > 0)", ["alpha"], None, None, []),
        ],
        index_rows=[
            ("z_unique", "CREATE UNIQUE INDEX z_unique", ["zeta"], None, False),
            ("a_unique", "CREATE UNIQUE INDEX a_unique", ["alpha"], None, False),
        ],
    )
    client = PostgresLiveSourceClient(_ConstraintConnection(cursor))

    constraints = client.get_constraints(URN)

    keys = [(constraint.kind, constraint.name) for constraint in constraints]
    assert keys == sorted(keys)
