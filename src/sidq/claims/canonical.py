"""Lossless PostgreSQL constraint representation and conservative comparison.

Database DDL is the authority during reconciliation.  This module therefore
keeps every constraint, even when its predicate is unfamiliar or unparsable:
recognised shapes improve evidence for people, but never decide whether a
constraint is represented.  Equivalence is deliberately limited to a small,
decidable ladder so uncertain SQL never becomes a false accusation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast

import sqlglot
from sqlglot import exp


@dataclass(frozen=True, slots=True)
class Shape:
    """A human-readable predicate family and the facts safely extracted from it."""

    name: str
    column: str | None = None
    columns: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    lower: str | None = None
    upper: str | None = None
    lower_inclusive: bool | None = None
    upper_inclusive: bool | None = None
    operator: str | None = None
    value: str | None = None
    function: str | None = None
    pattern: str | None = None
    target_table: str | None = None
    target_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizedConstraint:
    """One enforced constraint, with a stable identity even when opaque."""

    kind: str
    columns: tuple[str, ...]
    canonical: str | None
    fingerprint: str
    shape: Shape | None
    raw_ddl: str
    is_partial: bool = False
    partial_predicate: str | None = None


_COMPARISON_FLIPS: dict[type[exp.Expression], type[exp.Expression]] = {
    exp.GT: exp.LT,
    exp.GTE: exp.LTE,
    exp.LT: exp.GT,
    exp.LTE: exp.GTE,
}
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def canonicalize(sql: str) -> str | None:
    """Return a stable rendering of a PostgreSQL predicate, or ``None``.

    ``sql`` may be a CHECK body or a complete ``CHECK (...)`` definition.  The
    AST, rather than a token pattern, is normalised because PostgreSQL routinely
    adds casts and parentheses to definitions it returns from its catalog.
    """
    if not isinstance(sql, str) or not sql.strip():
        return None
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return None
    if not isinstance(tree, exp.Expression):
        return None
    predicate = _check_predicate(tree) or tree
    try:
        return _normalise_tree(predicate).sql(dialect="postgres")
    except (ValueError, TypeError, AttributeError):
        return None


def equivalent(left: str, right: str) -> tuple[bool, str]:
    """Compare two predicates through T0--T2, abstaining beyond them.

    T0 is textual identity after harmless whitespace/case normalisation.  T1
    compares canonical AST renderings.  T2 applies only the two listed,
    equivalence-preserving rewrites; all other AST differences are undecided.
    """
    if _raw_fingerprint(left) == _raw_fingerprint(right):
        return True, "identical"

    left_canonical = canonicalize(left)
    right_canonical = canonicalize(right)
    if left_canonical is None or right_canonical is None:
        return False, "undetermined"
    if left_canonical == right_canonical:
        return True, "canonical"

    left_rewritten = _rewrite_canonical(left)
    right_rewritten = _rewrite_canonical(right)
    if left_rewritten is not None and left_rewritten == right_rewritten:
        return True, "rewrite"
    return False, "undetermined"


def normalize_constraint(
    kind: str,
    columns: tuple[str, ...],
    raw_ddl: str,
    *,
    is_partial: bool = False,
    partial_predicate: str | None = None,
) -> NormalizedConstraint:
    """Build one record from LiveConstraint-shaped fields without rejecting it.

    The function intentionally accepts fields instead of importing the live
    source type at runtime, keeping schema acquisition independent from
    comparison.  For a CHECK, only a real ``CHECK (...)`` DDL wrapper is
    canonicalised; arbitrary non-CHECK input remains opaque.
    """
    safe_kind = kind if isinstance(kind, str) else str(kind)
    safe_columns = _safe_columns(columns)
    safe_ddl = raw_ddl if isinstance(raw_ddl, str) else str(raw_ddl)
    predicate = _parse_constraint_predicate(safe_kind, safe_ddl)
    canonical = _canonical_tree(predicate) if predicate is not None else None
    fingerprint_source = (
        canonical if canonical is not None else _normalise_raw(safe_ddl)
    )
    fingerprint = _fingerprint(safe_kind, fingerprint_source)
    shape = _detect_shape(safe_kind, safe_columns, predicate)
    return NormalizedConstraint(
        kind=safe_kind,
        columns=safe_columns,
        canonical=canonical,
        fingerprint=fingerprint,
        shape=shape,
        raw_ddl=safe_ddl,
        is_partial=bool(is_partial),
        partial_predicate=partial_predicate,
    )


def _parse_constraint_predicate(kind: str, raw_ddl: str) -> exp.Expression | None:
    if not raw_ddl.strip():
        return None
    try:
        tree = sqlglot.parse_one(raw_ddl, dialect="postgres")
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        if kind.casefold() != "foreign_key":
            return None
        return _parse_foreign_key(raw_ddl)
    if not isinstance(tree, exp.Expression):
        return None
    if kind.casefold() == "check":
        return _check_predicate(tree)
    return tree


def _parse_foreign_key(raw_ddl: str) -> exp.ForeignKey | None:
    """Parse a standalone catalog FK through its valid CREATE TABLE context."""
    try:
        wrapper = sqlglot.parse_one(
            f"CREATE TABLE sidq_constraint_parse ({raw_ddl})", dialect="postgres"
        )
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return None
    return next(wrapper.find_all(exp.ForeignKey), None)


def _check_predicate(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, exp.Anonymous) or tree.name.casefold() != "check":
        return None
    if len(tree.expressions) != 1:
        return None
    return _unwrap_parens(tree.expressions[0])


def _normalise_tree(tree: exp.Expression) -> exp.Expression:
    # ``transform`` visits a replacement before its former children.  Repeating
    # this pass removes PostgreSQL's nested cast/parenthesis wrappers completely.
    normal = tree
    while normal.find(exp.Cast) is not None or normal.find(exp.Paren) is not None:
        normal = normal.transform(_strip_casts_and_parens)
    normal = normal.transform(_normalise_identifier)
    normal = normal.transform(_convert_any_array_to_in)
    normal = normal.transform(_remove_double_negation)
    normal = normal.transform(_orient_comparison)
    normal = normal.transform(_sort_in_values)
    return normal.transform(_sort_boolean_operands)


def _strip_casts_and_parens(node: exp.Expression) -> exp.Expression:
    if isinstance(node, (exp.Cast, exp.Paren)):
        return node.this
    return node


def _normalise_identifier(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Identifier) and not node.quoted:
        return exp.Identifier(this=node.this.casefold(), quoted=False)
    return node


def _convert_any_array_to_in(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.EQ) or not isinstance(node.expression, exp.Any):
        return node
    array = _unwrap_parens(node.expression.this)
    if not isinstance(array, exp.Array):
        return node
    return exp.In(this=node.this, expressions=list(array.expressions))


def _remove_double_negation(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Not) and isinstance(node.this, exp.Not):
        return node.this.this
    return node


def _orient_comparison(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, _COMPARISONS):
        return node
    if isinstance(node.this, exp.Column) or not isinstance(node.expression, exp.Column):
        return node
    flipped = _COMPARISON_FLIPS.get(type(node))
    if flipped is not None:
        return flipped(this=node.expression, expression=node.this)
    if isinstance(node, (exp.EQ, exp.NEQ)):
        return type(node)(this=node.expression, expression=node.this)
    return node


def _sort_in_values(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, exp.In):
        return node
    values = sorted(node.expressions, key=_render_key)
    deduplicated: list[exp.Expression] = []
    previous: str | None = None
    for value in values:
        key = _render_key(value)
        if key != previous:
            deduplicated.append(value)
            previous = key
    return exp.In(this=node.this, expressions=deduplicated)


def _sort_boolean_operands(node: exp.Expression) -> exp.Expression:
    if not isinstance(node, (exp.And, exp.Or)):
        return node
    operands = sorted(_flatten_boolean(node, type(node)), key=_render_key)
    return _combine_boolean(type(node), operands)


def _flatten_boolean(
    tree: exp.Expression, expression_type: type[exp.Expression]
) -> list[exp.Expression]:
    if isinstance(tree, expression_type):
        return _flatten_boolean(tree.this, expression_type) + _flatten_boolean(
            tree.expression, expression_type
        )
    return [tree]


def _combine_boolean(
    expression_type: type[exp.Expression], operands: list[exp.Expression]
) -> exp.Expression:
    result = operands[0]
    for operand in operands[1:]:
        result = expression_type(this=result, expression=operand)
    return result


def _rewrite_canonical(sql: str) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, dialect="postgres")
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return None
    if not isinstance(tree, exp.Expression):
        return None
    predicate = _check_predicate(tree) or tree
    normal = _normalise_tree(predicate)
    rewritten = (
        _rewrite_interval(normal) or _rewrite_disjunction_to_set(normal) or normal
    )
    return _canonical_tree(rewritten)


def _rewrite_interval(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, exp.And):
        return None
    bounds = _range_bounds(tree)
    if bounds is None:
        return None
    column, lower, upper, lower_inclusive, upper_inclusive = bounds
    if not lower_inclusive or not upper_inclusive:
        return None
    return exp.Between(this=column, low=lower, high=upper)


def _rewrite_disjunction_to_set(tree: exp.Expression) -> exp.Expression | None:
    if not isinstance(tree, exp.Or):
        return None
    values: list[exp.Expression] = []
    column: exp.Column | None = None
    for operand in _flatten_boolean(tree, exp.Or):
        if not isinstance(operand, exp.EQ) or not isinstance(operand.this, exp.Column):
            return None
        if not isinstance(operand.expression, exp.Literal):
            return None
        if column is None:
            column = operand.this
        elif _render_key(column) != _render_key(operand.this):
            return None
        values.append(operand.expression)
    return exp.In(this=column, expressions=values) if column is not None else None


def _detect_shape(
    kind: str, columns: tuple[str, ...], predicate: exp.Expression | None
) -> Shape | None:
    folded_kind = kind.casefold()
    if folded_kind == "not_null":
        return Shape(
            name="not_null",
            column=columns[0] if len(columns) == 1 else None,
            columns=columns,
        )
    if folded_kind in {"unique", "primary_key"}:
        return Shape(name="unique", columns=columns)
    if folded_kind == "foreign_key":
        return _foreign_key_shape(columns, predicate)
    if folded_kind != "check" or predicate is None:
        return None
    return _check_shape(_normalise_tree(predicate))


def _foreign_key_shape(
    columns: tuple[str, ...], predicate: exp.Expression | None
) -> Shape:
    target_table: str | None = None
    target_columns: tuple[str, ...] = ()
    if isinstance(predicate, exp.ForeignKey):
        reference = predicate.args.get("reference")
        if isinstance(reference, exp.Reference) and isinstance(
            reference.this, exp.Schema
        ):
            target_table = reference.this.this.sql(dialect="postgres")
            target_columns = tuple(
                item.name
                for item in reference.this.expressions
                if isinstance(item, exp.Identifier)
            )
    return Shape(
        name="foreign_key",
        columns=columns,
        target_table=target_table,
        target_columns=target_columns,
    )


def _check_shape(tree: exp.Expression) -> Shape | None:
    if (
        isinstance(tree, exp.Is)
        and isinstance(tree.this, exp.Column)
        and isinstance(tree.expression, exp.Null)
        and tree.args.get("negate")
    ):
        return Shape(name="not_null", column=_column_name(tree.this))
    if (
        isinstance(tree, exp.Not)
        and isinstance(tree.this, exp.Is)
        and isinstance(tree.this.this, exp.Column)
        and isinstance(tree.this.expression, exp.Null)
    ):
        return Shape(name="not_null", column=_column_name(tree.this.this))

    if isinstance(tree, exp.Or):
        nullable = _nullable_guard(tree)
        if nullable is not None:
            return nullable
        accepted = _accepted_values_from_or(tree)
        if accepted is not None:
            return accepted
        return Shape(name="conditional", columns=_column_names(tree))

    if isinstance(tree, exp.In) and isinstance(tree.this, exp.Column):
        values = _literal_values(tree.expressions)
        if values is not None:
            return Shape(
                name="accepted_values", column=_column_name(tree.this), values=values
            )

    if isinstance(tree, exp.Between) and isinstance(tree.this, exp.Column):
        return Shape(
            name="range",
            column=_column_name(tree.this),
            lower=_render_key(tree.args["low"]),
            upper=_render_key(tree.args["high"]),
            lower_inclusive=True,
            upper_inclusive=True,
        )

    if isinstance(tree, exp.And):
        bounds = _range_bounds(tree)
        if bounds is not None:
            column, lower, upper, lower_inclusive, upper_inclusive = bounds
            return Shape(
                name="range",
                column=_column_name(column),
                lower=_render_key(lower),
                upper=_render_key(upper),
                lower_inclusive=lower_inclusive,
                upper_inclusive=upper_inclusive,
            )

    if isinstance(tree, (exp.RegexpLike, exp.Like)) and isinstance(
        tree.this, exp.Column
    ):
        return Shape(
            name="pattern",
            column=_column_name(tree.this),
            pattern=_render_key(tree.expression),
        )

    if isinstance(tree, _COMPARISONS):
        names = _column_names(tree)
        if len(names) >= 2:
            return Shape(name="cross_column", columns=names, operator=_operator(tree))
        function = next(tree.find_all(exp.Func), None)
        if function is not None:
            return Shape(
                name="function",
                column=names[0] if names else None,
                columns=names,
                operator=_operator(tree),
                function=function.sql_name().casefold(),
            )
        if isinstance(tree.this, exp.Column):
            return Shape(
                name="comparison",
                column=_column_name(tree.this),
                operator=_operator(tree),
                value=_render_key(tree.expression),
            )
    return None


def _nullable_guard(tree: exp.Or) -> Shape | None:
    for null_side, inner in (
        (tree.this, tree.expression),
        (tree.expression, tree.this),
    ):
        if not isinstance(null_side, exp.Is) or not isinstance(
            null_side.this, exp.Column
        ):
            continue
        if not isinstance(null_side.expression, exp.Null):
            continue
        column = _column_name(null_side.this)
        if column in _column_names(inner):
            return Shape(
                name="nullable_guard", column=column, columns=_column_names(inner)
            )
    return None


def _accepted_values_from_or(tree: exp.Or) -> Shape | None:
    values: list[exp.Expression] = []
    column: exp.Column | None = None
    for operand in _flatten_boolean(tree, exp.Or):
        if not isinstance(operand, exp.EQ) or not isinstance(operand.this, exp.Column):
            return None
        if not isinstance(operand.expression, exp.Literal):
            return None
        if column is None:
            column = operand.this
        elif _render_key(column) != _render_key(operand.this):
            return None
        values.append(operand.expression)
    literal_values = _literal_values(values)
    if column is None or literal_values is None:
        return None
    return Shape(
        name="accepted_values", column=_column_name(column), values=literal_values
    )


def _range_bounds(
    tree: exp.And,
) -> tuple[exp.Column, exp.Expression, exp.Expression, bool, bool] | None:
    lower: tuple[exp.Column, exp.Expression, bool] | None = None
    upper: tuple[exp.Column, exp.Expression, bool] | None = None
    for operand in _flatten_boolean(tree, exp.And):
        if not isinstance(operand, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            return None
        if not isinstance(operand.this, exp.Column):
            return None
        if isinstance(operand, (exp.GT, exp.GTE)):
            lower = (operand.this, operand.expression, isinstance(operand, exp.GTE))
        else:
            upper = (operand.this, operand.expression, isinstance(operand, exp.LTE))
    if lower is None or upper is None or _render_key(lower[0]) != _render_key(upper[0]):
        return None
    return lower[0], lower[1], upper[1], lower[2], upper[2]


def _literal_values(expressions: list[exp.Expression]) -> tuple[str, ...] | None:
    if not all(isinstance(item, exp.Literal) for item in expressions):
        return None
    return tuple(sorted(_literal_value(item) for item in expressions))


def _literal_value(literal: exp.Expression) -> str:
    assert isinstance(literal, exp.Literal)
    return literal.this


def _column_names(tree: exp.Expression) -> tuple[str, ...]:
    names: list[str] = []
    for column in tree.find_all(exp.Column):
        name = _column_name(column)
        if name not in names:
            names.append(name)
    return tuple(names)


def _column_name(column: exp.Column) -> str:
    return column.sql(dialect="postgres")


def _operator(tree: exp.Expression) -> str:
    operators: dict[type[exp.Binary], str] = {
        exp.EQ: "=",
        exp.NEQ: "!=",
        exp.GT: ">",
        exp.GTE: ">=",
        exp.LT: "<",
        exp.LTE: "<=",
    }
    return operators[cast(type[exp.Binary], type(tree))]


def _canonical_tree(tree: exp.Expression) -> str | None:
    try:
        return _normalise_tree(tree).sql(dialect="postgres")
    except (ValueError, TypeError, AttributeError):
        return None


def _unwrap_parens(tree: exp.Expression) -> exp.Expression:
    while isinstance(tree, exp.Paren):
        tree = tree.this
    return tree


def _render_key(tree: exp.Expression) -> str:
    return tree.sql(dialect="postgres")


def _safe_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(columns, (tuple, list)):
        return tuple(str(column) for column in columns)
    return ()


def _normalise_raw(raw_ddl: str) -> str:
    return " ".join(raw_ddl.split()).casefold()


def _fingerprint(kind: str, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"{kind.casefold()}:{digest}"


def _raw_fingerprint(sql: str) -> str | None:
    if not isinstance(sql, str) or not sql.strip():
        return None
    return _fingerprint("check", _normalise_raw(sql))
