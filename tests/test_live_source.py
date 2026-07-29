from __future__ import annotations

import pytest

from sidq.graph.live_source import PostgresLiveSourceClient, _relation_from_urn

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
