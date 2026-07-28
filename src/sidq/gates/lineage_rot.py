"""Compare DataHub's stored column lineage with the SQL that now produces it.

This gate is intentionally conservative.  It only reports rot after both sides of
an edge are resolved at column granularity; every unsupported or ambiguous case
is evidence that the lineage is unverifiable, not evidence that the catalog lies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidq.gates.base import graph_unavailable
from sidq.graph.client import GraphClient, LineageResult
from sidq.models import Evidence, TouchedAsset


@dataclass(frozen=True, slots=True)
class _SourceColumn:
    table: str
    column: str


@dataclass(frozen=True, slots=True)
class _Output:
    sources: tuple[_SourceColumn, ...]
    expression: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Relation:
    outputs: Mapping[str, _Output] | None
    table: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _Claims:
    edges: tuple[tuple[str, str, str], ...]
    source_urns: tuple[str, ...]


class LineageRotGate:
    """Detect stored field-level edges which disagree with a model's SQL.

    ``sql_by_urn`` is useful for callers which already have model text in memory.
    Otherwise ``TouchedAsset.source_path`` is read.  Supplying SQL explicitly
    makes the gate deterministic in tests and avoids coupling it to the resolver.
    """

    id = "lineage_rot"

    def __init__(self, sql_by_urn: Mapping[str, str] | None = None, *, dialect: str | None = None) -> None:
        self._sql_by_urn = dict(sql_by_urn or {})
        self._dialect = dialect

    def collect(self, change: Sequence[TouchedAsset], graph: GraphClient) -> list[Evidence]:
        evidence: list[Evidence] = []
        for asset in sorted(change, key=lambda item: item.urn):
            sql, read_error = self._sql(asset)
            if read_error is not None:
                evidence.append(_unverifiable(asset.urn, read_error))
                continue
            assert sql is not None
            outputs, parse_error = _outputs(sql, self._dialect)
            if parse_error is not None:
                evidence.append(_unverifiable(asset.urn, parse_error))
                continue
            try:
                dataset = graph.get_dataset(asset.urn)
                catalog_fields = {field.path for field in dataset.fields} if dataset is not None else set()
            except Exception as error:  # noqa: BLE001 - graph transports may raise arbitrary errors
                evidence.append(graph_unavailable(asset.urn, error))
                continue
            for output_name in sorted(set(outputs) | catalog_fields):
                # A catalog field with no projection is the most direct form of
                # rot: retain its stored claims, but record that no SQL expression
                # produces the output any more.
                output = outputs.get(output_name, _Output((), None))
                subject = f"{asset.urn}#{output_name}"
                if output.reason is not None:
                    evidence.append(_unverifiable(subject, output.reason))
                    continue
                try:
                    claimed = _claimed_edges(graph, asset.urn, output_name)
                except _Unverifiable as error:
                    evidence.append(_unverifiable(subject, str(error)))
                    continue
                except Exception as error:  # noqa: BLE001 - MCP transports may raise arbitrary errors
                    evidence.append(graph_unavailable(subject, error))
                    continue
                source_urns, resolve_error = _source_urns(output, asset, graph, claimed.source_urns)
                if resolve_error is not None:
                    evidence.append(_unverifiable(subject, resolve_error))
                    continue
                computed, output_error = _computed_edges(asset.urn, output_name, output, source_urns)
                if output_error is not None:
                    evidence.append(_unverifiable(subject, output_error))
                    continue
                _diff_edges(evidence, subject, claimed.edges, computed, output.expression)
        return evidence

    def _sql(self, asset: TouchedAsset) -> tuple[str | None, str | None]:
        if asset.urn in self._sql_by_urn:
            return self._sql_by_urn[asset.urn], None
        try:
            return Path(asset.source_path).read_text(encoding="utf-8"), None
        except (OSError, UnicodeError) as error:
            return None, f"model SQL could not be read: {type(error).__name__}"


def _outputs(sql: str, dialect: str | None) -> tuple[dict[str, _Output], str | None]:
    if "{{" in sql or "{%" in sql:
        return {}, "SQL contains unresolved template or macro syntax"
    try:
        import sqlglot

        statements = sqlglot.parse(sql, read=dialect)
    except Exception as error:  # noqa: BLE001 - sqlglot has several parser exception types
        return {}, f"SQL could not be parsed: {type(error).__name__}"
    if len(statements) != 1:
        return {}, "SQL must contain exactly one query"
    return _query_outputs(statements[0], {}), None


def _query_outputs(query: Any, inherited_ctes: Mapping[str, _Relation]) -> dict[str, _Output]:
    """Walk projections, joins, CTEs, and aliases into physical source columns."""
    from sqlglot import exp

    if isinstance(query, exp.Subquery):
        return _query_outputs(query.this, inherited_ctes)
    if not isinstance(query, exp.Select):
        return {"<query>": _Output((), query.sql(), f"unsupported SQL expression: {type(query).__name__}")}

    ctes = dict(inherited_ctes)
    with_clause = query.args.get("with_")
    if with_clause is not None:
        for cte in with_clause.expressions:
            name = cte.alias_or_name
            if not name:
                continue
            cte_outputs = _query_outputs(cte.this, ctes)
            ctes[name.casefold()] = _Relation(cte_outputs)

    relations: dict[str, _Relation] = {}
    from_clause = query.args.get("from_")
    if from_clause is not None:
        _add_relation(relations, from_clause.this, ctes)
    for join in query.args.get("joins") or ():
        _add_relation(relations, join.this, ctes)

    outputs: dict[str, _Output] = {}
    for projection in query.expressions:
        name = projection.alias_or_name
        if not name or name == "*":
            outputs["<star>"] = _Output((), projection.sql(), "SELECT * cannot be resolved without a schema")
            continue
        if projection.find(exp.Star) is not None:
            outputs[name] = _Output((), projection.sql(), "SELECT * cannot be resolved without a schema")
            continue
        sources: list[_SourceColumn] = []
        reason: str | None = None
        for column in projection.find_all(exp.Column):
            resolved, column_reason = _resolve_column(column, relations)
            if column_reason is not None:
                reason = column_reason
                break
            sources.extend(resolved)
        outputs[name] = _Output(
            tuple(sorted(set(sources), key=lambda item: (item.table, item.column))),
            projection.sql(),
            reason,
        )
    return outputs


def _add_relation(relations: dict[str, _Relation], item: Any, ctes: Mapping[str, _Relation]) -> None:
    from sqlglot import exp

    alias = item.alias_or_name if hasattr(item, "alias_or_name") else ""
    if not alias:
        return
    if isinstance(item, exp.Subquery):
        relations[alias.casefold()] = _Relation(_query_outputs(item.this, ctes))
    elif isinstance(item, exp.Table):
        cte = ctes.get(item.name.casefold()) if item.name else None
        relations[alias.casefold()] = cte or _Relation(None, _table_name(item))
    else:
        relations[alias.casefold()] = _Relation(None, reason=f"unsupported FROM expression: {type(item).__name__}")


def _resolve_column(column: Any, relations: Mapping[str, _Relation]) -> tuple[tuple[_SourceColumn, ...], str | None]:
    qualifier = column.table.casefold() if column.table else ""
    if qualifier:
        relation = relations.get(qualifier)
        if relation is None:
            return (), f"unresolvable relation alias: {column.table}"
    elif len(relations) == 1:
        relation = next(iter(relations.values()))
    else:
        return (), f"unqualified column is ambiguous: {column.name}"
    if relation.reason is not None:
        return (), relation.reason
    if relation.outputs is None:
        if relation.table is None:
            return (), f"unresolvable column: {column.sql()}"
        return (_SourceColumn(relation.table, column.name),), None
    output = relation.outputs.get(column.name)
    if output is None:
        return (), f"CTE or subquery has no output column: {column.name}"
    if output.reason is not None:
        return (), output.reason
    return output.sources, None


def _table_name(table: Any) -> str:
    # sqlglot represents four-part identifiers as a Dot inside ``table.this``;
    # ``table.name`` alone would silently drop the penultimate component.
    leaf = table.this.sql() if getattr(table, "this", None) is not None else table.name
    return ".".join(str(part) for part in (table.catalog, table.db, leaf) if part)


def _source_urns(
    output: _Output, asset: TouchedAsset, graph: GraphClient, claimed_urns: Sequence[str]
) -> tuple[dict[str, str], str | None]:
    tables = sorted({source.table for source in output.sources})
    resolved: dict[str, str] = {}
    for table in tables:
        claimed_candidates = {urn for urn in claimed_urns if _urn_matches_table(urn, table)}
        if len(claimed_candidates) == 1:
            resolved[table] = next(iter(claimed_candidates))
            continue
        if len(claimed_candidates) > 1:
            return {}, f"source table maps to multiple catalog datasets: {table}"
        candidates = {
            reference.dataset_urn
            for source in output.sources
            for reference in asset.referenced_fields
            if source.table == table and reference.field_path == source.column and _urn_matches_table(reference.dataset_urn, table)
        }
        if len(candidates) != 1:
            try:
                candidate = graph.find_dataset(table)
            except Exception as error:  # noqa: BLE001
                return {}, f"source dataset lookup failed for {table}: {type(error).__name__}"
            if candidate is not None and _urn_matches_table(candidate, table):
                candidates.add(candidate)
        if len(candidates) != 1:
            return {}, f"source table could not be resolved exactly: {table}"
        resolved[table] = next(iter(candidates))
    return resolved, None


def _urn_matches_table(urn: str, table: str) -> bool:
    match = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)$", urn)
    if match is None:
        return False
    relation = match.group(1).casefold()
    wanted = table.casefold()
    return relation == wanted or relation.endswith(f".{wanted}")


def _computed_edges(
    target_urn: str, output: str, lineage: _Output, source_urns: Mapping[str, str]
) -> tuple[tuple[tuple[str, str, str], ...], str | None]:
    edges: list[tuple[str, str, str]] = []
    for source in lineage.sources:
        urn = source_urns.get(source.table)
        if urn is None:
            return (), f"source table could not be resolved exactly: {source.table}"
        edges.append((urn, source.column, output))
    return tuple(sorted(set(edges))), None


class _Unverifiable(RuntimeError):
    pass


def _claimed_edges(graph: GraphClient, target_urn: str, target_column: str) -> _Claims:
    """Read target-side, stored fine-grained lineage with the official MCP call.

    The narrow public graph protocol predates this gate and only includes
    downstream traversal.  MCPGraphClient deliberately owns its caller, so use it
    here without widening unrelated project interfaces.  Test/in-process clients
    can expose ``get_upstream`` with the same LineageResult shape.
    """
    get_upstream = getattr(graph, "get_upstream", None)
    if callable(get_upstream):
        result = get_upstream(target_urn, 1, column=target_column)
        return _edges_from_result(result, target_column)
    caller = getattr(graph, "_tool_caller", None)
    if not callable(caller):
        raise _Unverifiable("graph client cannot retrieve target-side column lineage")
    raw = caller(
        "get_lineage",
        {"urn": target_urn, "upstream": True, "max_hops": 1, "max_results": 100, "column": target_column},
    )
    return _edges_from_mcp(raw, target_column)


def _edges_from_result(result: LineageResult, target_column: str) -> _Claims:
    if result.granularity != "column":
        raise _Unverifiable("graph did not return column-level lineage")
    if not isinstance(result.columns, Mapping):
        raise _Unverifiable("graph column-lineage response has no source columns")
    return _Claims(
        tuple(
            sorted(
                {
                    (urn, source_column, target_column)
                    for urn in result.urns
                    for source_column in result.columns.get(urn, ())
                    if isinstance(source_column, str)
                }
            )
        ),
        tuple(sorted(set(result.urns))),
    )


def _edges_from_mcp(raw: Any, target_column: str) -> _Claims:
    document = raw if isinstance(raw, Mapping) else {}
    metadata = document.get("metadata") if isinstance(document.get("metadata"), Mapping) else {}
    if metadata.get("queryType") != "column-level-lineage":
        raise _Unverifiable("graph did not return column-level lineage")
    upstreams = document.get("upstreams") if isinstance(document.get("upstreams"), Mapping) else {}
    results = upstreams.get("searchResults") if isinstance(upstreams.get("searchResults"), list) else []
    edges: set[tuple[str, str, str]] = set()
    source_urns: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping):
            continue
        entity = item.get("entity") if isinstance(item.get("entity"), Mapping) else {}
        urn = entity.get("urn")
        columns = item.get("lineageColumns")
        if not isinstance(urn, str) or not isinstance(columns, list):
            continue
        source_urns.add(urn)
        for source_column in columns:
            if isinstance(source_column, str):
                edges.add((urn, source_column, target_column))
    return _Claims(tuple(sorted(edges)), tuple(sorted(source_urns)))


def _diff_edges(
    evidence: list[Evidence],
    subject: str,
    claimed: Sequence[tuple[str, str, str]],
    computed: Sequence[tuple[str, str, str]],
    expression: str | None,
) -> None:
    claimed_set = set(claimed)
    computed_set = set(computed)
    computed_detail = [_edge(edge) for edge in sorted(computed_set)]
    for edge in sorted(claimed_set - computed_set):
        evidence.append(
            Evidence(
                "lineage_rot_missing",
                subject,
                {"claimed_edge": _edge(edge), "computed_edges": computed_detail, "sql_expression": expression, "confidence": "high"},
            )
        )
    for edge in sorted(computed_set - claimed_set):
        evidence.append(
            Evidence(
                "lineage_rot_extra",
                subject,
                {"claimed_edge": None, "computed_edges": computed_detail, "sql_expression": expression, "confidence": "high"},
            )
        )


def _edge(edge: tuple[str, str, str]) -> dict[str, str]:
    source_urn, source_column, target_column = edge
    return {"source_dataset": source_urn, "source_column": source_column, "target_column": target_column}


def _unverifiable(subject: str, reason: str) -> Evidence:
    return Evidence("lineage_unverifiable", subject, {"reason": reason, "confidence": "none"})
