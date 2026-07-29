"""Deterministic SQL verification for proposed documentation claims."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from sidq.graph.live_source import LiveSourceClient, _relation_from_urn
from sidq.models import Evidence

from .models import Claim


@dataclass(frozen=True, slots=True)
class CompiledClaim:
    count_sql: str
    sample_sql: str


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    status: Literal["holds", "violated", "unverifiable"]
    evidence: Evidence
    violating_row_count: int | None
    sample: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


def compile_claim(claim: Claim, relation: tuple[str, ...] | str) -> CompiledClaim:
    """Compile a narrow, read-only pair of count/sample queries for a claim."""
    table = _quote_relation(relation)
    column = _quote_identifier(claim.column)

    match claim.type:
        case "unique":
            violation_groups = (
                f"SELECT COUNT(*) AS group_count FROM {table} WHERE {column} IS NOT NULL "
                f"GROUP BY {column} HAVING COUNT(*) > 1"
            )
            return CompiledClaim(
                f"SELECT COALESCE(SUM(group_count), 0) AS violating_row_count FROM ({violation_groups}) AS violations",
                f"SELECT {column} AS value, COUNT(*) AS row_count FROM {table} WHERE {column} IS NOT NULL "
                f"GROUP BY {column} HAVING COUNT(*) > 1 ORDER BY value LIMIT 10",
            )
        case "not_null":
            predicate = f"{column} IS NULL"
        case "accepted_values":
            if not claim.values:
                raise ValueError("accepted_values claims require values")
            values = ", ".join(_quote_literal(value) for value in claim.values)
            predicate = f"{column} IS NOT NULL AND {column} NOT IN ({values})"
        case "relationships":
            if claim.expr is None:
                raise ValueError(
                    "relationships claims require expr as target_relation.target_column"
                )
            target_relation, target_column = _relationship_target(claim.expr)
            predicate = (
                f"source.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {target_relation} AS target "
                f"WHERE target.{target_column} = source.{column})"
            )
            table = f"{table} AS source"
        case "expression":
            if claim.expr is None:
                raise ValueError("expression claims require expr")
            expression = _validated_expression(claim.expr)
            predicate = f"(NOT ({expression}) OR ({expression}) IS NULL)"
        case _:
            raise ValueError(f"unsupported claim type: {claim.type!r}")

    return CompiledClaim(
        f"SELECT COUNT(*) AS violating_row_count FROM {table} WHERE {predicate}",
        f"SELECT {column} AS value FROM {table} WHERE {predicate} LIMIT 10",
    )


class ClaimVerifier:
    """Run compiled claims against a supplied live-source DB-API connection, read-only."""

    def __init__(
        self,
        source: LiveSourceClient,
        connection_or_factory: Any | Callable[[], Any] | None = None,
    ) -> None:
        self._source = source
        self._connection_or_factory = connection_or_factory

    def verify(self, claim: Claim, urn: str) -> ClaimVerification:
        try:
            dataset = self._source.get_dataset(urn)
        except Exception as error:  # noqa: BLE001 - source transports may raise arbitrary errors
            return self._unverifiable(
                claim, urn, f"live source unavailable: {type(error).__name__}"
            )
        if dataset is None:
            return self._unverifiable(
                claim, urn, "dataset is not available from the live source"
            )
        if claim.column not in {field.path for field in dataset.fields}:
            return self._unverifiable(
                claim, urn, f"column is not present in the live source: {claim.column}"
            )
        relation = _relation_from_urn(urn)
        if relation is None:
            return self._unverifiable(
                claim, urn, "dataset URN does not identify a source relation"
            )
        try:
            compiled = compile_claim(claim, relation)
            connection = self._connection()
            count = _query_count(connection, compiled.count_sql)
            sample = _query_sample(connection, compiled.sample_sql)
        except Exception as error:  # noqa: BLE001 - failure must never be misreported as a violation
            return self._unverifiable(
                claim,
                urn,
                f"SQL verification could not run: {type(error).__name__}: {error}",
            )
        status: Literal["holds", "violated"] = "holds" if count == 0 else "violated"
        detail = _detail(claim, violating_row_count=count, sample=sample)
        return ClaimVerification(
            status=status,
            violating_row_count=count,
            sample=sample,
            evidence=Evidence(
                "doc_claim_holds" if status == "holds" else "doc_claim_violated",
                _subject(urn, claim),
                detail,
            ),
        )

    def _connection(self) -> Any:
        candidate = self._connection_or_factory
        if candidate is None:
            candidate = getattr(self._source, "_connection_or_factory", None)
        if candidate is None:
            candidate = getattr(self._source, "connection", None)
        if candidate is None:
            raise RuntimeError("query connection unavailable")
        # sqlite connections are callable for callback registration, but are already
        # usable DB-API connections. Prefer the DB-API shape over ``callable``.
        return candidate if hasattr(candidate, "cursor") else candidate()

    @staticmethod
    def _unverifiable(claim: Claim, urn: str, reason: str) -> ClaimVerification:
        return ClaimVerification(
            status="unverifiable",
            violating_row_count=None,
            reason=reason,
            evidence=Evidence(
                "doc_claim_unverifiable",
                _subject(urn, claim),
                _detail(claim, reason=reason),
            ),
        )


def verify_claim(
    claim: Claim,
    urn: str,
    source: LiveSourceClient,
    connection_or_factory: Any | Callable[[], Any] | None = None,
) -> ClaimVerification:
    """Convenience wrapper for one proposed claim."""
    return ClaimVerifier(source, connection_or_factory).verify(claim, urn)


def _subject(urn: str, claim: Claim) -> str:
    return f"{urn}#{claim.column}"


def _detail(claim: Claim, **result: Any) -> dict[str, Any]:
    detail = asdict(claim)
    detail.update(result)
    if detail.get("values") is not None:
        detail["values"] = list(detail["values"])
    if "sample" in detail:
        detail["sample"] = list(detail["sample"])
    return detail


def _query_count(connection: Any, sql: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        cursor.close()


def _query_sample(connection: Any, sql: str) -> tuple[dict[str, Any], ...]:
    cursor = connection.cursor()
    try:
        cursor.execute(sql)
        names = tuple(column[0] for column in cursor.description or ())
        return tuple(
            {
                str(name): _json_value(value)
                for name, value in zip(names, row, strict=True)
            }
            for row in cursor.fetchall()
        )
    finally:
        cursor.close()


def _json_value(value: Any) -> Any:
    return (
        value
        if value is None or isinstance(value, (str, int, float, bool))
        else str(value)
    )


_RELATION_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


def _quote_relation(relation: tuple[str, ...] | str) -> str:
    parts = tuple(relation.split(".")) if isinstance(relation, str) else relation
    if not parts or any(not _RELATION_PART.fullmatch(part) for part in parts):
        raise ValueError("relation must be a dot-separated SQL identifier")
    return ".".join(_quote_identifier(part) for part in parts)


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or not identifier:
        raise ValueError("identifier must be a non-empty string")
    return f'"{identifier.replace('"', '""')}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _relationship_target(expr: str) -> tuple[str, str]:
    parts = tuple(expr.split("."))
    if len(parts) < 2:
        raise ValueError("relationship target must include a relation and column")
    return _quote_relation(parts[:-1]), _quote_identifier(parts[-1])


def _validated_expression(value: str) -> str:
    try:
        statements = sqlglot.parse(value, read="postgres")
    except (
        Exception
    ) as error:  # pragma: no cover - parser errors vary by sqlglot release
        raise ValueError("expression is not valid SQL") from error
    if len(statements) != 1:
        raise ValueError("expression must contain exactly one SQL expression")
    expression = statements[0]
    if expression is None:
        raise ValueError("expression is not valid SQL")
    forbidden = (
        exp.Query,
        exp.Subquery,
        exp.Command,
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
    )
    if isinstance(expression, forbidden) or any(
        isinstance(node, forbidden) for node in expression.walk()
    ):
        raise ValueError("expression may not contain a query or command")
    return expression.sql(dialect="postgres")
