from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="module")
def miner() -> Any:
    path = Path(__file__).parents[1] / "scripts" / "mine_dbt_claims.py"
    spec = importlib.util.spec_from_file_location("mine_dbt_claims", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def row(sentence: str, claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "class": "positive",
        "input": {"sentence": sentence},
        "target": {"claim": claim},
        "source": {"collection_source": "dbt_schema_yml"},
    }


def test_accepted_values_keeps_only_the_values_stated_by_the_sentence(
    miner: Any,
) -> None:
    item = row(
        "Status must be one of: pending, shipped, delivered.",
        {
            "type": "accepted_values",
            "values": ["pending", "shipped", "delivered", "cancelled", "returned"],
        },
    )

    assert miner.expressed(item)
    assert item["target"]["claim"]["values"] == ["pending", "shipped", "delivered"]


def test_accepted_values_rejects_a_sentence_that_names_an_unknown_value(
    miner: Any,
) -> None:
    item = row(
        "Status must be one of: pending, shipped, unknown.",
        {"type": "accepted_values", "values": ["pending", "shipped", "delivered"]},
    )

    assert not miner.expressed(item)


def test_accepted_values_rejects_an_unknown_value_in_a_must_be_list(miner: Any) -> None:
    item = row(
        "Status must be pending or unknown.",
        {"type": "accepted_values", "values": ["pending", "shipped", "delivered"]},
    )

    assert not miner.expressed(item)


@pytest.mark.parametrize(
    ("sentence", "expr"),
    [
        ("Must be a positive amount.", "> 0"),
        ("Value must be non-negative.", ">= 0"),
        ("The name must not be blank.", "length(name) > 0"),
        ("The date must be in the future.", "due_date > CURRENT_DATE"),
        ("Must be at least 18.", ">= 18"),
        ("Must be no more than 20.", "<= 20"),
    ],
)
def test_expression_accepts_unambiguous_semantic_or_stated_bounds(
    miner: Any, sentence: str, expr: str
) -> None:
    assert miner.expressed(row(sentence, {"type": "expression", "expr": expr}))


@pytest.mark.parametrize(
    ("sentence", "claim"),
    [
        ("Player's first name", {"type": "expression", "expr": "> 0"}),
        ("Order status", {"type": "accepted_values", "values": ["pending", "shipped"]}),
        ("Filter by product SKU.", {"type": "expression", "expr": "> 0"}),
        (
            "Invalid status",
            {"type": "accepted_values", "values": ["pending", "shipped"]},
        ),
    ],
)
def test_unasserted_descriptions_remain_rejected(
    miner: Any, sentence: str, claim: dict[str, Any]
) -> None:
    assert not miner.expressed(row(sentence, claim))


@pytest.mark.parametrize(
    "sentence",
    ["Must be a large amount.", "Must be a reasonable amount.", "Must be short."],
)
def test_vague_quantities_remain_rejected(miner: Any, sentence: str) -> None:
    assert not miner.expressed(row(sentence, {"type": "expression", "expr": "> 0"}))
