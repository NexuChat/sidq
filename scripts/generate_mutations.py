"""Generate a deterministic SQL mutation corpus from the bundled dbt demo."""

from __future__ import annotations

import argparse
import difflib
import json
import random
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TypedDict

import sqlglot
from sqlglot import exp


class MutationRecord(TypedDict):
    id: str
    family: str
    intent: str
    model_path: str
    original_sql: str
    mutated_sql: str
    diff: str
    notes: str


Mutation = Callable[[str, random.Random], str | None]


def _select(sql: str) -> exp.Select | None:
    expression = sqlglot.parse_one(sql)
    return expression.find(exp.Select)


def _render(expression: exp.Expression) -> str:
    return expression.sql(pretty=True, dialect="postgres") + "\n"


def _projection_name(projection: exp.Expression) -> str | None:
    name = projection.alias_or_name
    return name if name and name != "*" else None


def output_columns(sql: str) -> tuple[str, ...]:
    """Return the declared output fields, excluding an intentionally opaque star."""
    select = _select(sql)
    if select is None:
        return ()
    return tuple(
        name
        for projection in select.expressions
        if (name := _projection_name(projection)) is not None
    )


def drop_selected_column(sql: str, rng: random.Random) -> str | None:
    select = _select(sql)
    if select is None or len(select.expressions) < 2:
        return None
    expressions = list(select.expressions)
    expressions.pop(rng.randrange(len(expressions)))
    select.set("expressions", expressions)
    return _render(select.root())


def rename_selected_column(sql: str, rng: random.Random) -> str | None:
    select = _select(sql)
    if select is None:
        return None
    candidates = [item for item in select.expressions if _projection_name(item)]
    if not candidates:
        return None
    chosen = rng.choice(candidates)
    name = _projection_name(chosen)
    assert name is not None
    chosen.replace(exp.alias_(chosen.copy(), f"{name}_renamed", copy=False))
    return _render(select.root())


def change_column_type_cast(sql: str, rng: random.Random) -> str | None:
    select = _select(sql)
    if select is None:
        return None
    candidates = [item for item in select.expressions if _projection_name(item)]
    if not candidates:
        return None
    chosen = rng.choice(candidates)
    name = _projection_name(chosen)
    assert name is not None
    inner = chosen.this.copy() if isinstance(chosen, exp.Alias) else chosen.copy()
    cast = exp.Cast(this=inner, to=exp.DataType.build(rng.choice(("TEXT", "BIGINT"))))
    chosen.replace(exp.alias_(cast, name, copy=False))
    return _render(select.root())


def expose_pii_tagged_column(sql: str, rng: random.Random) -> str | None:
    del rng
    select = _select(sql)
    if select is None:
        return None
    pii = next(
        (
            item
            for item in select.find_all(exp.Column)
            if any(word in item.name.lower() for word in ("email", "phone", "dob"))
        ),
        None,
    )
    if pii is None:
        return None
    select.append("expressions", exp.alias_(pii.copy(), "exposed_pii_value", copy=False))
    return _render(select.root())


def replace_explicit_select_with_star(sql: str, rng: random.Random) -> str | None:
    del rng
    select = _select(sql)
    if select is None or any(isinstance(item, exp.Star) for item in select.expressions):
        return None
    select.set("expressions", [exp.Star()])
    return _render(select.root())


def change_join_key(sql: str, rng: random.Random) -> str | None:
    expression = sqlglot.parse_one(sql)
    conditions = [item for item in expression.find_all(exp.EQ) if item.find(exp.Column)]
    if not conditions:
        return None
    condition = rng.choice(conditions)
    columns = list(condition.find_all(exp.Column))
    if len(columns) < 2:
        return None
    target = columns[-1]
    alternatives = ["id", "order_id", "customer_id", "country_id"]
    alternatives = [name for name in alternatives if name != target.name]
    target.set("this", exp.to_identifier(rng.choice(alternatives)))
    return _render(expression)


def delete_where_filter(sql: str, rng: random.Random) -> str | None:
    del rng
    select = _select(sql)
    if select is None or select.args.get("where") is None:
        return None
    select.set("where", None)
    return _render(select.root())


def change_aggregation_grain(sql: str, rng: random.Random) -> str | None:
    select = _select(sql)
    if select is None:
        return None
    group = select.args.get("group")
    if not isinstance(group, exp.Group) or not group.expressions:
        return None
    expressions = list(group.expressions)
    if len(expressions) > 1 and rng.randrange(2) == 0:
        expressions.pop(rng.randrange(len(expressions)))
    else:
        candidates = [
            item.copy()
            for item in select.find_all(exp.Column)
            if item.sql() not in {value.sql() for value in expressions}
        ]
        if not candidates:
            return None
        expressions.append(rng.choice(candidates))
    group.set("expressions", expressions)
    return _render(select.root())


def reference_nonexistent_upstream_column(sql: str, rng: random.Random) -> str | None:
    expression = sqlglot.parse_one(sql)
    columns = list(expression.find_all(exp.Column))
    if not columns:
        return None
    chosen = rng.choice(columns)
    chosen.set("this", exp.to_identifier("sidq_missing_upstream_column"))
    return _render(expression)


def reformat_whitespace(sql: str, rng: random.Random) -> str | None:
    del rng
    return sqlglot.parse_one(sql).sql(pretty=False, dialect="postgres") + "\n"


def add_or_remove_sql_comment(sql: str, rng: random.Random) -> str | None:
    del rng
    lines = sql.splitlines()
    if lines and lines[0].lstrip().startswith("-- sidq-benchmark:"):
        return "\n".join(lines[1:]).lstrip() + "\n"
    return "-- sidq-benchmark: metadata-only comment\n" + sql.rstrip() + "\n"


def rename_cte(sql: str, rng: random.Random) -> str | None:
    expression = sqlglot.parse_one(sql)
    with_clause = expression.args.get("with_")
    if not isinstance(with_clause, exp.With) or not with_clause.expressions:
        return None
    cte = rng.choice(list(with_clause.expressions))
    old_name = cte.alias_or_name
    if not old_name:
        return None
    new_name = f"{old_name}_local"
    cte.set("alias", exp.TableAlias(this=exp.to_identifier(new_name)))
    for table in expression.find_all(exp.Table):
        if table.name == old_name:
            table.set("this", exp.to_identifier(new_name))
    return _render(expression)


def reorder_select_list(sql: str, rng: random.Random) -> str | None:
    select = _select(sql)
    if select is None or len(select.expressions) < 2:
        return None
    expressions = list(select.expressions)
    first = rng.randrange(len(expressions))
    second = (first + 1 + rng.randrange(len(expressions) - 1)) % len(expressions)
    expressions[first], expressions[second] = expressions[second], expressions[first]
    select.set("expressions", expressions)
    return _render(select.root())


def rename_local_alias(sql: str, rng: random.Random) -> str | None:
    expression = sqlglot.parse_one(sql)
    aliases = [
        alias
        for alias in expression.find_all(exp.TableAlias)
        if alias.parent is not None and isinstance(alias.parent, exp.Table)
    ]
    if not aliases:
        return None
    alias = rng.choice(aliases)
    old_name = alias.this.name if isinstance(alias.this, exp.Identifier) else None
    if not old_name:
        return None
    new_name = f"{old_name}_local"
    alias.set("this", exp.to_identifier(new_name))
    for column in expression.find_all(exp.Column):
        if column.table == old_name:
            column.set("table", exp.to_identifier(new_name))
    return _render(expression)


def add_non_pii_derived_column(sql: str, rng: random.Random) -> str | None:
    del rng
    select = _select(sql)
    if select is None:
        return None
    select.append(
        "expressions",
        exp.alias_(
            exp.Cast(this=exp.Literal.number(1), to=exp.DataType.build("INTEGER")),
            "sidq_benchmark_flag",
            copy=False,
        ),
    )
    return _render(select.root())


FAMILIES: dict[str, tuple[str, Mutation, str]] = {
    "drop_selected_column": ("harmful", drop_selected_column, "remove one output"),
    "rename_selected_column": ("harmful", rename_selected_column, "rename output"),
    "change_column_type_cast": ("harmful", change_column_type_cast, "cast output"),
    "expose_pii_tagged_column": ("harmful", expose_pii_tagged_column, "duplicate PII"),
    "replace_explicit_select_with_star": ("harmful", replace_explicit_select_with_star, "select star"),
    "change_join_key": ("harmful", change_join_key, "alter join predicate"),
    "delete_where_filter": ("harmful", delete_where_filter, "remove WHERE"),
    "change_aggregation_grain": ("harmful", change_aggregation_grain, "alter GROUP BY"),
    "reference_nonexistent_upstream_column": ("harmful", reference_nonexistent_upstream_column, "unknown field"),
    "reformat_whitespace": ("benign", reformat_whitespace, "format only"),
    "add_or_remove_sql_comment": ("benign", add_or_remove_sql_comment, "comment only"),
    "rename_cte": ("benign", rename_cte, "local CTE only"),
    "reorder_select_list": ("benign", reorder_select_list, "projection order"),
    "rename_local_alias": ("benign", rename_local_alias, "local table alias"),
    "add_non_pii_derived_column": ("benign", add_non_pii_derived_column, "constant field"),
}


def _diff(path: str, original: str, mutated: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def generate_records(
    demo_root: Path, *, seed: int, per_family: int
) -> Iterable[MutationRecord]:
    """Yield stable mutation records in a deliberately fixed traversal order."""
    rng = random.Random(seed)
    for model in sorted(demo_root.glob("models/**/*.sql")):
        original = model.read_text(encoding="utf-8")
        path = model.relative_to(demo_root).as_posix()
        for family, (intent, mutate, notes) in FAMILIES.items():
            for number in range(per_family):
                mutated = mutate(original, rng)
                if mutated is None or mutated == original:
                    continue
                yield {
                    "id": f"{family}:{path}:{number:04d}",
                    "family": family,
                    "intent": intent,
                    "model_path": path,
                    "original_sql": original,
                    "mutated_sql": mutated,
                    "diff": _diff(path, original, mutated),
                    "notes": notes,
                }


def write_records(path: Path, records: Iterable[MutationRecord]) -> Counter[str]:
    ordered = sorted(records, key=lambda record: record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for record in ordered:
            output.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return Counter(record["family"] for record in ordered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--demo-root", type=Path, default=Path("demo/dbt"))
    parser.add_argument("--per-family", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.per_family < 1:
        raise SystemExit("--per-family must be at least 1")
    histogram = write_records(
        args.out,
        generate_records(args.demo_root.resolve(), seed=args.seed, per_family=args.per_family),
    )
    print(f"wrote {sum(histogram.values())} mutations to {args.out}")
    for family in FAMILIES:
        print(f"{family}: {histogram[family]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
