from __future__ import annotations

import pytest

from sidq.claims.canonical import canonicalize, equivalent, normalize_constraint


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("x > 0", "0 < x"),
        ("a AND b", "b AND a"),
        ("(status)::text = 'new'::text", "status = 'new'"),
        ("x IN ('b', 'a')", "x IN ('a', 'b')"),
        ("x = ANY(ARRAY['a', 'b'])", "x IN ('a', 'b')"),
        ("NOT (NOT x)", "x"),
    ],
)
def test_canonicalize_normalizes_equivalent_postgres_predicates(
    left: str, right: str
) -> None:
    assert canonicalize(left) == canonicalize(right)


def test_canonicalize_folds_only_unquoted_identifiers() -> None:
    assert canonicalize("MIXEDCASE = 1") == canonicalize("mixedcase = 1")
    assert canonicalize('"MixedCase" = 1') != canonicalize("mixedcase = 1")


@pytest.mark.parametrize(
    "ddl",
    [
        "CHECK (((status)::text = ANY ((ARRAY['new'::character varying, 'paid'::character varying])::text[])))",
        "CHECK (total = (subtotal + tax))",
        "CHECK (end_date IS NULL OR end_date >= start_date)",
        "CHECK (length(referral_code) = 8)",
        "CHECK (country::text = upper(country::text))",
        "CHECK (credit_score >= 300 AND credit_score <= 850)",
        "CHECK (email ~ '^[^@]+@[^@]+$'::text)",
    ],
)
def test_postgres_normalized_check_definitions_are_represented(ddl: str) -> None:
    constraint = normalize_constraint("check", (), ddl)

    assert constraint.canonical is not None
    assert constraint.fingerprint.startswith("check:")


@pytest.mark.parametrize(
    "ddl",
    ["CHECK (this is not sql (((", "", "UNIQUE (x)"],
)
def test_normalize_constraint_is_total_for_opaque_checks(ddl: str) -> None:
    constraint = normalize_constraint("check", ("x",), ddl)

    assert constraint.canonical is None
    assert constraint.shape is None
    assert constraint.fingerprint.startswith("check:")


def test_fingerprints_are_stable_for_canonical_semantics() -> None:
    left = normalize_constraint("check", ("x",), "CHECK (x > 0)")
    equivalent_spelling = normalize_constraint("check", ("x",), "CHECK (0 < x)")
    different = normalize_constraint("check", ("x",), "CHECK (x > 1)")

    assert left.fingerprint == equivalent_spelling.fingerprint
    assert left.fingerprint != different.fingerprint


@pytest.mark.parametrize(
    ("kind", "columns", "ddl", "name", "attributes"),
    [
        (
            "not_null",
            ("x",),
            '"x" NOT NULL',
            "not_null",
            {"column": "x", "columns": ("x",)},
        ),
        (
            "unique",
            ("a", "b"),
            "UNIQUE (a, b)",
            "unique",
            {"columns": ("a", "b")},
        ),
        (
            "foreign_key",
            ("a", "b"),
            "FOREIGN KEY (a, b) REFERENCES target_table(c, d)",
            "foreign_key",
            {
                "columns": ("a", "b"),
                "target_table": "target_table",
                "target_columns": ("c", "d"),
            },
        ),
        (
            "check",
            ("status",),
            "CHECK (status = ANY(ARRAY['paid', 'new']))",
            "accepted_values",
            {"column": "status", "values": ("new", "paid")},
        ),
        (
            "check",
            ("pct",),
            "CHECK (pct >= 0 AND pct <= 100)",
            "range",
            {
                "column": "pct",
                "lower": "0",
                "upper": "100",
                "lower_inclusive": True,
                "upper_inclusive": True,
            },
        ),
        (
            "check",
            ("x",),
            "CHECK (0 < x)",
            "comparison",
            {"column": "x", "operator": ">", "value": "0"},
        ),
        (
            "check",
            ("end_date", "start_date"),
            "CHECK (end_date >= start_date)",
            "cross_column",
            {"columns": ("end_date", "start_date"), "operator": ">="},
        ),
        (
            "check",
            ("email",),
            "CHECK (email ~ '^[^@]+@[^@]+$')",
            "pattern",
            {"column": "email", "pattern": "'^[^@]+@[^@]+$'"},
        ),
        (
            "check",
            ("referral_code",),
            "CHECK (length(referral_code) = 8)",
            "function",
            {"column": "referral_code", "function": "length", "operator": "="},
        ),
        (
            "check",
            ("end_date", "start_date"),
            "CHECK (end_date IS NULL OR end_date >= start_date)",
            "nullable_guard",
            {"column": "end_date", "columns": ("end_date", "start_date")},
        ),
        (
            "check",
            ("status", "total"),
            "CHECK (status <> 'refunded' OR total > 0)",
            "conditional",
            {"columns": ("status", "total")},
        ),
    ],
)
def test_recognised_shapes_retain_their_parameters(
    kind: str,
    columns: tuple[str, ...],
    ddl: str,
    name: str,
    attributes: dict[str, object],
) -> None:
    shape = normalize_constraint(kind, columns, ddl).shape

    assert shape is not None
    assert shape.name == name
    for attribute, expected in attributes.items():
        assert getattr(shape, attribute) == expected


@pytest.mark.parametrize(
    ("left", "right", "tier"),
    [
        ("x > 0", "x > 0", "identical"),
        ("x > 0", "0 < x", "canonical"),
        ("x >= 0 AND x <= 100", "x BETWEEN 0 AND 100", "rewrite"),
        ("x = 'a' OR x = 'b'", "x IN ('a', 'b')", "rewrite"),
    ],
)
def test_equivalent_uses_the_decidable_ladder(left: str, right: str, tier: str) -> None:
    assert equivalent(left, right) == (True, tier)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("x > 0", "x < 0"),
        ("length(referral_code) = 8", "length(referral_code) = 9"),
    ],
)
def test_equivalent_abstains_when_the_ladder_cannot_decide(
    left: str, right: str
) -> None:
    assert equivalent(left, right) == (False, "undetermined")
