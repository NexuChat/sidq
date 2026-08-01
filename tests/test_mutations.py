from __future__ import annotations

import gzip
import json
from pathlib import Path

import sqlglot

from scripts import generate_mutations, label_mutations

ROOT = Path(__file__).parents[1]
DEMO = ROOT / "demo" / "dbt"


def _records(seed: int = 0) -> list[dict[str, str]]:
    return list(generate_mutations.generate_records(DEMO, seed=seed, per_family=1))


def test_every_mutation_family_produces_parseable_sql() -> None:
    records = _records()
    present = {record["family"] for record in records}

    # Every family must find at least one applicable model in the demo project.
    # An inapplicable family is a silently untested slice of the benchmark, so
    # this asserts full applicability rather than documenting gaps.
    assert set(generate_mutations.FAMILIES) - present == set()
    for record in records:
        assert sqlglot.parse_one(record["mutated_sql"]), record["id"]


def test_generator_is_deterministic_for_a_fixed_seed(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    generate_mutations.write_records(first, _records(seed=7))
    generate_mutations.write_records(second, _records(seed=7))

    assert first.read_bytes() == second.read_bytes()


def test_regression_artifact_packaging_is_compact_and_deterministic(
    tmp_path: Path,
) -> None:
    records = [
        {"id": "b", "verdict": "PASS", "context": {}},
        {"id": "a", "verdict": "BLOCK", "context": {}},
    ]
    first = tmp_path / "first.jsonl.gz"
    second = tmp_path / "second.jsonl.gz"

    label_mutations.write_regression_artifact(first, records)
    label_mutations.write_regression_artifact(second, reversed(records))

    assert first.read_bytes() == second.read_bytes()
    with gzip.open(first, mode="rt", encoding="utf-8") as handle:
        assert [json.loads(line)["id"] for line in handle] == ["a", "b"]


def test_benign_mutation_keeps_output_columns_and_drop_removes_one() -> None:
    records = _records()
    baseline = {
        record["model_path"]: generate_mutations.output_columns(record["original_sql"])
        for record in records
    }
    benign = next(
        record for record in records if record["family"] == "reformat_whitespace"
    )
    dropped = next(
        record for record in records if record["family"] == "drop_selected_column"
    )

    assert (
        generate_mutations.output_columns(benign["mutated_sql"])
        == baseline[benign["model_path"]]
    )
    assert (
        generate_mutations.output_columns(dropped["mutated_sql"])
        != baseline[dropped["model_path"]]
    )


def test_labeller_verdict_does_not_use_generator_intent(tmp_path: Path) -> None:
    record = next(record for record in _records() if record["intent"] == "harmful")

    def fixed_engine(**_: object) -> object:
        return label_mutations.EngineResult("WARN", "FIXED", (), ())

    labelled = label_mutations.label_record(
        record,
        demo_root=DEMO,
        fixture_dir=ROOT / "tests" / "fixtures" / "graph",
        engine=fixed_engine,
        scratch_root=tmp_path,
    )
    relabelled = label_mutations.label_record(
        {**record, "id": f"{record['id']}:intent-flipped", "intent": "benign"},
        demo_root=DEMO,
        fixture_dir=ROOT / "tests" / "fixtures" / "graph",
        engine=fixed_engine,
        scratch_root=tmp_path,
    )

    assert labelled["intent"] == "harmful"
    assert labelled["verdict"] == "WARN"
    assert labelled["reason_code"] == "FIXED"
    assert relabelled["verdict"] == labelled["verdict"]


def test_context_is_limited_to_fast_prefilter_inputs(tmp_path: Path) -> None:
    record = next(record for record in _records() if record["intent"] == "benign")

    def fixed_engine(**_: object) -> object:
        return label_mutations.EngineResult("BLOCK", "PRIVATE", (), ())

    labelled = label_mutations.label_record(
        record,
        demo_root=DEMO,
        fixture_dir=ROOT / "tests" / "fixtures" / "graph",
        engine=fixed_engine,
        scratch_root=tmp_path,
    )

    assert set(labelled["context"]) == label_mutations.CONTEXT_KEYS
    assert not (
        {"verdict", "reason_code", "rule_ids", "evidence_kinds"}
        & set(labelled["context"])
    )
    json.dumps(labelled, sort_keys=True)
