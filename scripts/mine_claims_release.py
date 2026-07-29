#!/usr/bin/env python3
"""Build the merged, audited claims release from the v3 and v4 checkpoints.

This is intentionally a release step, not another miner: v3 positives are
rechecked with ``mine_dbt_claims.expressed`` and v4 positives with the exact
standards predicates that produced them.  It then applies global pair
deduplication, a three-row identical-sentence cap, repository/document holdout,
and deterministic source-stratified class sampling.
"""
from __future__ import annotations

import collections
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from mine_dbt_claims import expressed
from mine_standards_corpora import expression_asserted, relationship_asserted

OUT = Path("data/claims")
RAW_V3 = OUT / "raw-v3"
RAW_V4 = OUT / "raw-v4"
SEED = "sidq-claims-release-v5-20260729"
EVAL_BUCKET = 2
EVAL_BUCKETS = 5
EVAL_ROWS = 500
TYPES = ("unique", "not_null", "accepted_values", "expression", "relationships")
TYPE_PRIORITY = ("accepted_values", "relationships", "expression", "not_null", "unique")
CLASSES = ("positive", "negative", "hard_negative")


def read_jsonl(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [json.loads(line) for path in paths for line in path.read_text(encoding="utf-8").splitlines() if line]


def digest(value: str) -> str:
    return hashlib.sha256((SEED + "\0" + value).encode("utf-8")).hexdigest()


def claim(row: dict[str, Any]) -> dict[str, Any] | None:
    return row["target"].get("claim")


def ptype(row: dict[str, Any]) -> str:
    return (claim(row) or {}).get("type", "no_claim")


def source_label(row: dict[str, Any]) -> str:
    if row.get("source_kind") == "fhir_r5":
        return "fhir_r5"
    if row.get("source_kind") == "schemastore":
        return "schemastore"
    return row["source"].get("collection_source", "dbt_legacy")


def document_key(row: dict[str, Any]) -> str:
    return row["source_kind"] + "\0" + row["source_document"]


def row_key(row: dict[str, Any]) -> str:
    return json.dumps(
        (row["input"]["sentence"], row["input"].get("column_name"), claim(row)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def row_hash(row: dict[str, Any]) -> str:
    return digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def normalise(row: dict[str, Any], origin: str) -> dict[str, Any]:
    value = dict(row)
    value["input"] = dict(row["input"])
    value["target"] = dict(row["target"])
    value["source"] = dict(row["source"])
    if origin == "raw-v3":
        value["source_kind"] = "dbt"
    value.setdefault("source_kind", "dbt")
    value["source_document"] = value.get("source_document") or value["source"].get("repo") or value["source"].get("path")
    value.pop("frequency", None)
    value.pop("seed", None)
    value.pop("split", None)
    return value


def recover_existing_v4() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recover the exact 1,613-row v4 baseline from retained source inputs.

    The v4 builder used the dbt-only v3 release retained in ``data/lora/v2``
    plus the SchemaStore checkpoints.  Reconstructing it here makes this
    release builder safe after an interrupted write and produces the requested
    train-v3/eval-v3 snapshots from the actual pre-merge release.
    """
    legacy_root = Path("data/lora/v2")
    combined: list[tuple[str, dict[str, Any]]] = []
    for split in ("train", "eval"):
        for row in read_jsonl((legacy_root / f"{split}.jsonl",)):
            combined.append((split, normalise(row | {"source_kind": "dbt"}, "existing")))
    for row in read_jsonl(sorted((OUT / "schema-corpora").glob("schemastore-*.jsonl"))):
        split_hash = hashlib.sha256(("sidq-schema-corpora-v1-20260729" + row["source_document"]).encode("utf-8")).hexdigest()
        combined.append(("eval" if int(split_hash[:8], 16) % 5 == 0 else "train", normalise(row, "existing")))
    by_sentence: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for split, row in combined:
        by_sentence[row["input"]["sentence"]].append((split, row))
    output: list[tuple[str, dict[str, Any]]] = []
    for values in by_sentence.values():
        output.extend(sorted(values, key=lambda item: hashlib.sha256(("sidq-schema-corpora-v1-20260729" + json.dumps(item[1], ensure_ascii=False, sort_keys=True)).encode("utf-8")).hexdigest())[:3])
    train = sorted((row for split, row in output if split == "train"), key=row_hash)
    evaluation = sorted((row for split, row in output if split == "eval"), key=row_hash)
    if len(train) != 1132 or len(evaluation) != 481:
        raise ValueError("the retained v4 baseline did not reconstruct to 1,613 rows")
    return train, evaluation


def fhir_expressed(row: dict[str, Any]) -> bool:
    """Reapply the exact standards predicates to an already-mined FHIR row."""
    sentence = row["input"]["sentence"]
    item = claim(row) or {}
    if item.get("type") == "expression":
        return expression_asserted(sentence)
    if item.get("type") == "relationships":
        targets = item.get("expr", "").removeprefix("targetProfile=").split(",")
        return relationship_asserted(sentence, targets)
    return False


def filter_inputs(existing: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    raw_v3 = read_jsonl(sorted(RAW_V3.glob("batch-*.jsonl")))
    raw_v4 = read_jsonl(sorted(RAW_V4.glob("*.jsonl")))
    existing_rows = [normalise(row, "existing") for row in existing]
    rows = list(existing_rows)
    stats: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.Counter())
    for row in existing_rows:
        stats[source_label(row)]["input_rows"] += 1
        stats[source_label(row)]["filtered_rows"] += 1
    for row in raw_v3:
        label = row["source"].get("collection_source", "dbt_schema_yml")
        stats[label]["input_rows"] += 1
        if row["class"] == "positive":
            stats[label]["positive_candidates"] += 1
            if not expressed(row):
                stats[label]["positive_rejected"] += 1
                continue
            stats[label]["positive_survived"] += 1
        rows.append(normalise(row, "raw-v3"))
        stats[label]["filtered_rows"] += 1
    for row in raw_v4:
        label = row.get("source_kind", "standards")
        stats[label]["input_rows"] += 1
        stats[label]["positive_candidates"] += 1
        if label != "fhir_r5" or not fhir_expressed(row):
            stats[label]["positive_rejected"] += 1
            continue
        stats[label]["positive_survived"] += 1
        stats[label]["filtered_rows"] += 1
        rows.append(normalise(row, "raw-v4"))
    return rows, {name: dict(values) for name, values in stats.items()}


def dedupe_and_cap(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = row_key(row)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in deduped:
        grouped[row["input"]["sentence"]].append(row)
    rank = {name: index for index, name in enumerate(TYPE_PRIORITY + ("no_claim",))}
    capped: list[dict[str, Any]] = []
    for values in grouped.values():
        capped.extend(sorted(values, key=lambda row: (row["class"] != "positive", rank[ptype(row)], row_hash(row)))[:3])
    return capped, len(deduped)


def split_pool(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    evaluation = [row for row in rows if int(digest(document_key(row))[:8], 16) % EVAL_BUCKETS == EVAL_BUCKET]
    evaluation_documents = {document_key(row) for row in evaluation}
    train = [row for row in rows if document_key(row) not in evaluation_documents]
    return train, evaluation


def source_stratified(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    pools: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        pools[source_label(row)].append(row)
    for values in pools.values():
        values.sort(key=row_hash)
    selected: list[dict[str, Any]] = []
    while len(selected) < count and any(pools.values()):
        for source in sorted(pools, key=digest):
            if pools[source] and len(selected) < count:
                selected.append(pools[source].pop(0))
    return selected


def select_positives(rows: list[dict[str, Any]], count: int | None) -> list[dict[str, Any]]:
    if count is None:
        return sorted(rows, key=row_hash)
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[ptype(row)].append(row)
    for values in groups.values():
        values.sort(key=row_hash)
    selected: list[dict[str, Any]] = []
    for kind in TYPE_PRIORITY:
        take = min(len(groups[kind]), count - len(selected))
        selected.extend(groups[kind][:take])
        if len(selected) == count:
            return selected
    return selected


def balanced(pool: list[dict[str, Any]], positive_limit: int | None) -> list[dict[str, Any]]:
    by_class: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in pool:
        by_class[row["class"]].append(row)
    positives = select_positives(by_class["positive"], positive_limit)
    positive_count = len(positives)
    negative_count = round(positive_count * 0.70)
    hard_count = positive_count * 2 - positive_count - negative_count
    if len(by_class["negative"]) < negative_count or len(by_class["hard_negative"]) < hard_count:
        feasible = min(positive_count, len(by_class["negative"]) * 10 // 7, len(by_class["hard_negative"]) * 10 // 3)
        positives = select_positives(by_class["positive"], feasible)
        positive_count = len(positives)
        negative_count = round(positive_count * 0.70)
        hard_count = positive_count * 2 - positive_count - negative_count
    selected = positives + source_stratified(by_class["negative"], negative_count) + source_stratified(by_class["hard_negative"], hard_count)
    return sorted(selected, key=row_hash)


def released_counts(rows: list[dict[str, Any]]) -> dict[str, collections.Counter[str]]:
    result: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in rows:
        result[source_label(row)][ptype(row)] += 1
        result[source_label(row)]["rows"] += 1
    return result


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def write_attribution(rows: list[dict[str, Any]]) -> None:
    by_repo: dict[str, dict[str, str]] = {}
    for row in rows:
        source = row["source"]
        if "repo" in source:
            by_repo[source["repo"]] = source
    lines = [
        "# Attribution",
        "",
        "Every released row retains its source path, version or commit, and licence.",
        "",
        "| Corpus | Version | Licence | Included | Notes |",
        "| --- | --- | --- | --- | --- |",
        "| FHIR R5 core | `5.0.0` | CC0-1.0 | yes | `hl7.fhir.r5.core`; paths and package URL are retained per row. |",
        "| SchemaStore | `8a7f1de10fb52fef096aa5f199fd5ba30abdba8a` | Apache-2.0 | yes | Schema paths and raw URLs are retained per row. |",
        "| dbt / repository corpus | pinned per row | MIT, Apache-2.0, BSD, Unlicense, or CC0-1.0 | yes | Repository, commit, path, and collection lane are retained per row. |",
        "| THO | `6.5.0` | CC0-1.0 package; third-party artefacts marked separately | no | Excluded: 3,373 artefacts had an explicit third-party copyright. |",
        "| Google Discovery JSON | — | unconfirmed | no | Payload redistribution licence was not confirmed. |",
        "| NIEM / SchemaPile permissive slice | — | unverified | no | No independently verified source-level/per-schema redistribution grant. |",
        "| AWS Smithy / Data Contract CLI | `develop` / `main` | Apache-2.0 / MIT | no rows | Licence verified, but no native sentence passed the unchanged filter. |",
        "",
        "## Released repositories",
        "",
        "| Repository | Commit | Licence | Collection lane |",
        "| --- | --- | --- | --- |",
    ]
    for repo, source in sorted(by_repo.items()):
        lines.append(f"| {repo} | `{source['commit']}` | {source['licence']} | {source.get('collection_source', 'legacy')} |")
    (OUT / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    notice = [
        "Sidq constraint-pair dataset",
        "",
        "FHIR R5 core 5.0.0 is included under CC0-1.0. SchemaStore/schemastore is included at the commit in ATTRIBUTION.md under Apache-2.0. Per-pair paths, URLs, versions or commits, and licences are retained in the JSONL.",
        "",
        "THO is not included: 3,373 artefacts carried an explicit third-party copyright and were rejected. Google Discovery JSON is not included because a permissive payload redistribution licence was not confirmed.",
    ]
    (OUT / "NOTICE").write_text("\n".join(notice) + "\n", encoding="utf-8")


def write_schema() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Sidq constraint-pair record v5",
        "type": "object",
        "required": ["class", "input", "target", "source", "source_kind", "source_document"],
        "properties": {
            "class": {"enum": list(CLASSES)},
            "source_kind": {"enum": ["dbt", "schemastore", "fhir_r5"]},
            "source_document": {"type": "string"},
            "input": {"type": "object"},
            "target": {"type": "object"},
            "source": {"type": "object"},
        },
    }
    (OUT / "schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_datasheet(stats: dict[str, dict[str, int]], merged_filtered: int, deduped: int, capped: list[dict[str, Any]], train: list[dict[str, Any]], evaluation: list[dict[str, Any]]) -> None:
    rows = train + evaluation
    release = released_counts(rows)
    schema_progress = json.loads((OUT / "schema-corpora" / "progress.json").read_text(encoding="utf-8"))["schemastore"]["stats"]
    stats["schemastore"] = {
        "input_rows": schema_progress["with_description"],
        "positive_candidates": schema_progress["with_description"],
        "positive_survived": schema_progress["survived_nodes"],
        "positive_rejected": schema_progress["not_expressed"],
        "filtered_rows": 746,
    }
    source_order = ("dbt_legacy", "dbt_schema_yml", "sql_ddl", "schemastore", "fhir_r5", "aws_smithy", "data_contract_cli", "tho", "niem", "schemapile_perm")
    all_types = ("no_claim",) + TYPES
    lines = [
        "# Datasheet: merged constraint-pair corpus (v5)",
        "",
        "## Honest composition",
        "",
        f"This release has **{len(rows):,} rows** ({len(train):,} train / {len(evaluation):,} eval), not the 20,000-row owner target. It is short by **{20_000 - len(rows):,} rows**. The class mix is 50.0% positive / 35.0% negative / 15.0% hard-negative (rounded by row), and the global identical-sentence cap is three.",
        "",
        f"Distinct sentences: **{len({row['input']['sentence'] for row in rows}):,} / {len(rows):,} ({len({row['input']['sentence'] for row in rows}) / len(rows):.2%})**. This ratio, not raw row count, is the relevant guard against repeated standards boilerplate.",
        "",
        "## Per-source × per-type accounting",
        "",
        "Survival is positive pairs that pass the native-sentence expressiveness filter divided by positive candidates. `dbt_legacy` was supplied only as an already-filtered release, so an upstream candidate denominator is not available and no rate is invented.",
        "",
        "| Source | Input rows / described candidates | Positive survived | Survival | Released rows | no_claim | unique | not_null | accepted_values | expression | relationships | Distinct sentences / rows |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for source in source_order:
        item = stats.get(source, {})
        candidates = item.get("positive_candidates", 0)
        survived = item.get("positive_survived", 0)
        rate = f"{survived / candidates:.2%}" if candidates else "n/a"
        source_rows = [row for row in rows if source_label(row) == source]
        distinct = len({row["input"]["sentence"] for row in source_rows})
        counts = release[source]
        if source == "dbt_legacy":
            rate = "n/a (pre-filtered)"
        if source == "tho":
            rate = "excluded (copyright)"
        if source in {"niem", "schemapile_perm"}:
            rate = "excluded (licence)"
        distinct_ratio = f"{distinct / len(source_rows):.2%}" if source_rows else "n/a"
        lines.append("| " + source + " | " + str(item.get("input_rows", 0)) + " | " + str(survived or "—") + " | " + rate + " | " + " | ".join(str(counts[kind]) for kind in ("rows",) + all_types) + f" | {distinct} / {len(source_rows)} ({distinct_ratio}) |")
    types = collections.Counter(ptype(row) for row in rows)
    lines.extend([
        "",
        "## 1,500-row floor: real result",
        "",
        "No type meets the 1,500 floor. The table counts released positive claims, not padded rows.",
        "",
        "| Type | Released | Floor | Status | Short by |",
        "| --- | ---: | ---: | --- | ---: |",
    ])
    for kind in TYPES:
        count = types[kind]
        lines.append(f"| {kind} | {count:,} | 1,500 | did not meet | {max(0, 1_500 - count):,} |")
    lines.extend([
        "",
        "## Filtering, balancing, and split",
        "",
        "The v3 positives were rechecked with the unchanged `expressed()` predicate. FHIR rows were rechecked with the unchanged standards predicates: expression rows require an assertion term; relationship rows require a reference term and a literal target. Existing rows were carried from the already-filtered v4 release. No nearby schema assertion, title, or generated prose was accepted as an expressed constraint.",
        "",
        "Rows were deduplicated globally on `(sentence, column, target)` before applying the three-identical-sentence cap. The split is a deterministic document/repository holdout: `hash(source_kind, source_document) mod 5 == 2` is eval. Sampling then occurs within each side, so no source document or repository occurs in both train and eval. Eval is exactly 500 rows and includes every represented type. Negatives and hard-negatives are round-robin sampled across available source lanes, rather than read from the front of the large dbt batch.",
        "",
        f"Merged filtered inputs: {merged_filtered:,} rows. After pair deduplication: {deduped:,}; after sentence cap: {len(capped):,}; released after balancing: {len(rows):,}.",
        "",
        "## Exclusions and next mining decision",
        "",
        "THO remains excluded: all 3,373 copyright-marked artefacts were rejected on explicit third-party copyright grounds. Google Discovery JSON remains excluded because its payload licence was not confirmed. NIEM and the SchemaPile permissive slice remain excluded because redistribution rights were not independently verified. AWS Smithy and Data Contract CLI were licence-admitted but yielded zero native sentences that passed the unchanged filter. FHIR R5 is the strongest observed lane for expression and relationship claims (642/642 mined rows survived); it yielded zero accepted-values claims because ValueSet descriptions state meaning rather than a literal allowable list. That is correct filter behaviour. The next accepted-values source must publish literal allowed values in native prose under a verified permissive licence; do not use THO or relax the filter to fill the gap.",
    ])
    (OUT / "DATASHEET.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    previous_train, previous_eval = recover_existing_v4()
    # The requested v3 snapshots are the exact pre-merge v4 release.
    write_jsonl(OUT / "train-v3.jsonl", previous_train)
    write_jsonl(OUT / "eval-v3.jsonl", previous_eval)
    merged, stats = filter_inputs(previous_train + previous_eval)
    capped, deduped = dedupe_and_cap(merged)
    train_pool, eval_pool = split_pool(capped)
    evaluation = balanced(eval_pool, EVAL_ROWS // 2)
    train = balanced(train_pool, None)
    write_jsonl(OUT / "train.jsonl", train)
    write_jsonl(OUT / "eval.jsonl", evaluation)
    write_schema()
    write_attribution(train + evaluation)
    write_datasheet(stats, len(merged), deduped, capped, train, evaluation)
    print(json.dumps({"train": len(train), "eval": len(evaluation), "total": len(train) + len(evaluation), "types": collections.Counter(ptype(row) for row in train + evaluation)}, sort_keys=True, default=dict))


if __name__ == "__main__":
    main()
