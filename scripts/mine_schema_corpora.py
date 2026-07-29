#!/usr/bin/env python3
"""Mine licence-gated schema-description constraint pairs.

This file deliberately keeps collection and rebuilding in one resumable,
auditable command.  Invoke ``--self-test`` before a network crawl.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

OUT = Path("data/claims")
CHECKPOINTS = OUT / "schema-corpora"
PROGRESS = CHECKPOINTS / "progress.json"
SCHEMASTORE_REPO = "SchemaStore/schemastore"
SCHEMASTORE_ROOT = "src/schemas/"
SCHEMASTORE_LICENSE = "Apache-2.0"
SEED = "sidq-schema-corpora-v1-20260729"
BATCH_SIZE = 100
RARE_FLOORS = {"accepted_values": 1_500, "relationships": 1_500, "expression": 1_500}
KINDS = ("not_null", "unique", "accepted_values", "relationships", "expression")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`\"'])")
SPACE_RE = re.compile(r"\s+")


def clean_description(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = SPACE_RE.sub(" ", value).strip()
    return value if len(value) >= 8 else None


def sentences(value: str) -> list[str]:
    return [part.strip() for part in SENTENCE_RE.split(value) if len(part.strip()) >= 8] or [value]


def literal_in(value: Any, text: str) -> bool:
    if isinstance(value, bool):
        value = str(value).lower()
    elif value is None:
        value = "null"
    else:
        value = str(value)
    return bool(value and re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text, re.IGNORECASE))


def expresses_constraint(sentence: str, kind: str, evidence: list[Any]) -> bool:
    """True only when native prose states the executable constraint.

    This intentionally does not treat neighbouring schema syntax, titles, or
    concatenated value glosses as prose.  The call site passes one original
    sentence, never manufactured text.
    """
    text = SPACE_RE.sub(" ", sentence).casefold()
    if kind == "accepted_values":
        signal = re.search(r"\b(?:one of|only|allowed|valid|permitted|must be|may be|either)\b", text)
        return bool(signal and evidence and all(literal_in(value, text) for value in evidence))
    if kind == "not_null":
        return bool(
            re.search(r"\b(?:required|mandatory|not[ -]?null|must be present|must not be null|cannot be null|never null)\b", text)
            and not re.search(r"\b(?:not required|optional|nullable|may be null|can be null)\b", text)
        )
    if kind == "unique":
        return bool(re.search(r"\b(?:unique|distinct|no duplicates?|non-duplicated)\b", text))
    if kind == "relationships":
        return bool(
            re.search(r"\b(?:references?|refers? to|foreign key|links? to)\b", text)
            and any(str(item).casefold() in text for item in evidence if str(item))
        )
    if kind == "expression":
        numbers = [item for item in evidence if isinstance(item, (int, float)) and not isinstance(item, bool)]
        if numbers:
            return bool(
                all(literal_in(item, text) for item in numbers)
                and re.search(r"(?:>=|<=|≥|≤|\b(?:at least|at most|minimum|maximum|between|range|multiple of|divisible by|greater than|less than)\b)", text)
            )
        pattern = str(evidence[0]) if evidence else ""
        return bool(pattern and literal_in(pattern, text) and re.search(r"\b(?:pattern|regex|regular expression|match(?:es)?)\b", text))
    return False


def run_self_test() -> None:
    """Executable acceptance tests for the no-adjacency rule."""
    assert expresses_constraint("One of PENDING, RUNNING or DONE.", "accepted_values", ["PENDING", "RUNNING", "DONE"])
    assert not expresses_constraint("Status of the job.", "accepted_values", ["PENDING", "RUNNING", "DONE"])
    assert expresses_constraint("The value must be at least 2 and no more than 10.", "expression", [2, 10])
    assert not expresses_constraint("The retry configuration.", "expression", [2, 10])
    assert expresses_constraint("This field is required.", "not_null", [])
    assert expresses_constraint("The parent reference refers to Customer.", "relationships", ["Customer"])
    assert not expresses_constraint("The parent identifier.", "relationships", ["Customer"])
    assert not expresses_constraint("PENDING means queued. DONE means finished.", "accepted_values", ["PENDING", "DONE"])


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def request_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sidq-schema-corpora-miner/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def source_commit() -> str:
    """Resolve a reproducible SHA, falling back to codeload during API limits."""
    try:
        metadata = json.loads(request_bytes(f"https://api.github.com/repos/{SCHEMASTORE_REPO}/commits/master"))
        sha = metadata.get("sha")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass
    archive = zipfile.ZipFile(io.BytesIO(request_bytes(f"https://codeload.github.com/{SCHEMASTORE_REPO}/zip/master")))
    sha = archive.comment.decode("ascii", errors="ignore")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("SchemaStore archive did not expose a full commit SHA")
    return sha


def fetch_schemastore(commit: str) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(request_bytes(f"https://codeload.github.com/{SCHEMASTORE_REPO}/zip/{commit}")))
    licence = next((member for member in archive.namelist() if member.count("/") == 1 and member.lower().endswith("/license")), None)
    if licence is None or b"Apache License" not in archive.read(licence):
        raise ValueError("SchemaStore archive licence did not verify as Apache-2.0")
    return archive


def primitive_enum(value: Any) -> list[Any] | None:
    if not isinstance(value, list) or not value or not all(item is None or isinstance(item, (str, int, float, bool)) for item in value):
        return None
    return value


def column_name(path: tuple[str, ...]) -> str:
    useful = [part for part in path if part not in {"properties", "items", "$defs", "definitions", "allOf", "anyOf", "oneOf"}]
    return useful[-1] if useful else "root"


def reference_terms(reference: str) -> list[str]:
    return [part for part in re.split(r"[/#._-]+", reference) if len(part) >= 3 and part not in {"properties", "definitions", "schemas"}]


def constraints(document: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[str, dict[str, Any], str | None, list[Any], tuple[str, ...]]]:
    """Yield each executable constraint, with its native description and evidence."""
    if isinstance(document, dict):
        description = clean_description(document.get("description"))
        enum = primitive_enum(document.get("enum"))
        if enum is not None:
            yield "accepted_values", {"type": "accepted_values", "column": column_name(path), "values": enum}, description, enum, path
        elif isinstance(document.get("enum"), list):
            yield "accepted_values", {"type": "accepted_values", "column": column_name(path), "values": []}, description, [], path
        expression_keys = [key for key in ("pattern", "minimum", "maximum", "multipleOf") if key in document]
        if expression_keys:
            evidence: list[Any] = []
            chunks: list[str] = []
            for key in expression_keys:
                value = document[key]
                evidence.append(value)
                chunks.append(f"{key}={value}")
            yield "expression", {"type": "expression", "column": column_name(path), "expr": ";".join(chunks)}, description, evidence, path
        if isinstance(document.get("$ref"), str):
            reference = document["$ref"]
            yield "relationships", {"type": "relationships", "column": column_name(path), "expr": f"ref={reference}"}, description, reference_terms(reference), path
        if document.get("uniqueItems") is True:
            yield "unique", {"type": "unique", "column": column_name(path)}, description, [], path
        properties = document.get("properties")
        if isinstance(properties, dict) and isinstance(document.get("required"), list):
            for name in document["required"]:
                child = properties.get(name) if isinstance(name, str) else None
                child_description = clean_description(child.get("description")) if isinstance(child, dict) else None
                yield "not_null", {"type": "not_null", "column": name if isinstance(name, str) else column_name(path)}, child_description, [], path + (str(name),)
        for key, value in document.items():
            if key in {"description", "enum", "pattern", "minimum", "maximum", "multipleOf", "$ref", "uniqueItems", "required"}:
                continue
            if isinstance(value, (dict, list)):
                yield from constraints(value, path + (key,))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            if isinstance(value, (dict, list)):
                yield from constraints(value, path + (str(index),))


def source_record(commit: str, path: str) -> dict[str, str]:
    return {
        "source": "SchemaStore",
        "url": f"https://raw.githubusercontent.com/{SCHEMASTORE_REPO}/{commit}/{path}",
        "path": path,
        "commit": commit,
        "version": commit,
        "licence": SCHEMASTORE_LICENSE,
    }


def record(commit: str, path: str, kind: str, claim: dict[str, Any], sentence: str, schema_path: tuple[str, ...]) -> dict[str, Any]:
    return {
        "class": "positive",
        "input": {"sentence": sentence, "column_name": claim["column"], "table_name": path, "schema_context": "json_schema_path=" + "/".join(schema_path)},
        "target": {"claim": claim},
        "source_kind": "schemastore",
        "source_document": path,
        "source": source_record(commit, path),
    }


def empty_stats() -> dict[str, Any]:
    return {"documents": 0, "constrained_nodes": 0, "no_description": 0, "with_description": 0, "not_expressed": 0, "survived_nodes": 0, "unsupported_enum": 0, "by_type": {kind: {"constrained_nodes": 0, "no_description": 0, "with_description": 0, "not_expressed": 0, "survived_nodes": 0} for kind in KINDS}}


def mine_document(document: Any, commit: str, path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    stats = empty_stats(); stats["documents"] = 1
    for kind, claim, description, evidence, schema_path in constraints(document):
        stats["constrained_nodes"] += 1; stats["by_type"][kind]["constrained_nodes"] += 1
        if kind == "accepted_values" and not claim.get("values"):
            stats["unsupported_enum"] += 1
            continue
        if not description:
            stats["no_description"] += 1; stats["by_type"][kind]["no_description"] += 1
            continue
        stats["with_description"] += 1; stats["by_type"][kind]["with_description"] += 1
        accepted = [sentence for sentence in sentences(description) if expresses_constraint(sentence, kind, evidence)]
        if not accepted:
            stats["not_expressed"] += 1; stats["by_type"][kind]["not_expressed"] += 1
            continue
        stats["survived_nodes"] += 1; stats["by_type"][kind]["survived_nodes"] += 1
        rows.extend(record(commit, path, kind, claim, sentence, schema_path) for sentence in accepted)
    return rows, stats


def add_stats(total: dict[str, Any], current: dict[str, Any]) -> None:
    for key in ("documents", "constrained_nodes", "no_description", "with_description", "not_expressed", "survived_nodes", "unsupported_enum"):
        total[key] += current[key]
    for kind in KINDS:
        for key in total["by_type"][kind]:
            total["by_type"][kind][key] += current["by_type"][kind][key]


def initial_progress(commit: str, paths: list[str]) -> dict[str, Any]:
    return {
        "seed": SEED,
        "licence_decisions": {
            "discovery": {"included": False, "licence": "unconfirmed", "reason": "Discovery API terms bind use of the API; the CC-BY notice applies to the terms page, not confirmed Discovery JSON response payloads."},
            "schemastore": {"included": True, "licence": SCHEMASTORE_LICENSE, "repository": SCHEMASTORE_REPO, "commit": commit},
        },
        "schemastore": {"commit": commit, "paths": paths, "completed_batches": [], "stats": empty_stats()},
    }


def schema_paths(archive: zipfile.ZipFile) -> tuple[str, list[str]]:
    root = archive.namelist()[0].split("/", 1)[0] + "/"
    prefix = root + SCHEMASTORE_ROOT
    paths = [member.removeprefix(root) for member in archive.namelist() if member.startswith(prefix) and member.endswith(".json")]
    if not paths:
        raise ValueError("no JSON schema documents found in SchemaStore archive")
    return root, sorted(paths)


def collect_schemastore(progress: dict[str, Any]) -> None:
    state = progress["schemastore"]; commit = state["commit"]
    archive = fetch_schemastore(commit); root, paths = schema_paths(archive)
    if paths != state["paths"]:
        raise ValueError("pinned SchemaStore document list changed; remove checkpoints only after reviewing the commit")
    done = set(state["completed_batches"])
    batches = [paths[offset : offset + BATCH_SIZE] for offset in range(0, len(paths), BATCH_SIZE)]
    for number, batch in enumerate(batches, 1):
        if number in done:
            continue
        rows: list[dict[str, Any]] = []; batch_stats = empty_stats()
        for path in batch:
            try:
                document = json.loads(archive.read(root + path).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
                print(f"skip malformed {path}: {error}", file=sys.stderr)
                continue
            mined, stats = mine_document(document, commit, path); rows.extend(mined); add_stats(batch_stats, stats)
        write_jsonl(CHECKPOINTS / f"schemastore-{number:04d}.jsonl", rows)
        atomic_json(CHECKPOINTS / f"schemastore-{number:04d}.manifest.json", {"batch": number, "paths": batch, "stats": batch_stats, "rows": len(rows)})
        add_stats(state["stats"], batch_stats); state["completed_batches"].append(number); atomic_json(PROGRESS, progress)
        print(json.dumps({"batch": number, "documents": batch_stats["documents"], "survived_nodes": batch_stats["survived_nodes"], "no_description": batch_stats["no_description"], "rows": len(rows)}, sort_keys=True), flush=True)


def load_mined() -> list[dict[str, Any]]:
    return [json.loads(line) for path in sorted(CHECKPOINTS.glob("schemastore-*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines() if line]


def legacy_rows(name: str) -> list[dict[str, Any]]:
    path = OUT / f"{name}-v3.jsonl"
    return [json.loads(line) | {"source_kind": "dbt"} for line in path.read_text(encoding="utf-8").splitlines() if line]


def preserve_v3() -> None:
    for name in ("train", "eval"):
        source, backup = OUT / f"{name}.jsonl", OUT / f"{name}-v3.jsonl"
        if source.exists() and not backup.exists():
            shutil.copy2(source, backup)


def sentence_key(row: dict[str, Any]) -> str:
    return row["input"]["sentence"]


def stable_hash(value: str) -> str:
    return hashlib.sha256((SEED + value).encode("utf-8")).hexdigest()


def cap_rows(rows: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = collections.defaultdict(list)
    for split, row in rows:
        grouped[sentence_key(row)].append((split, row))
    result: list[tuple[str, dict[str, Any]]] = []
    for values in grouped.values():
        frequency = sum(row.get("frequency", 1) for _, row in values)
        result.extend((split, row | {"frequency": frequency}) for split, row in sorted(values, key=lambda item: stable_hash(json.dumps(item[1], sort_keys=True, ensure_ascii=False)))[:3])
    return result


def schema_split(path: str) -> str:
    return "eval" if int(stable_hash(path)[:8], 16) % 5 == 0 else "train"


def write_schema() -> None:
    atomic_json(OUT / "schema.json", {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Sidq constraint-pair record v4", "type": "object", "required": ["class", "input", "target", "source", "source_kind"], "properties": {"source_kind": {"enum": ["dbt", "discovery", "schemastore"]}, "source_document": {"type": "string"}, "input": {"type": "object"}, "target": {"type": "object"}, "source": {"type": "object"}}})


def type_counts(rows: Iterable[dict[str, Any]]) -> collections.Counter[str]:
    return collections.Counter(row["target"]["claim"]["type"] if row["target"]["claim"] else "no_claim" for row in rows)


def write_attribution(progress: dict[str, Any], train: list[dict[str, Any]], evaluation: list[dict[str, Any]]) -> None:
    dbt = train + evaluation
    dbt_repos = {(row["source"]["repo"], row["source"]["commit"], row["source"]["licence"]) for row in dbt if row["source_kind"] == "dbt"}
    state = progress["schemastore"]
    lines = ["# Attribution", "", "Every released pair retains per-pair provenance.  This index records the admitted corpora and their pinned versions.", "", "| Corpus | Version / commit | Licence | Included | Notes |", "| --- | --- | --- | --- | --- |", f"| SchemaStore | `{state['commit']}` | Apache-2.0 | yes | {SCHEMASTORE_REPO}; paths and raw URLs are recorded per pair. |", "| Google Discovery JSON | — | unconfirmed | no | Excluded: Discovery API terms govern API use; no permissive payload licence was confirmed. |", "", f"## dbt-derived input preserved from v3 ({len(dbt_repos)} repositories)", "", "| Repository | Commit | Licence |", "| --- | --- | --- |"]
    lines.extend(f"| {repo} | `{commit}` | {licence} |" for repo, commit, licence in sorted(dbt_repos))
    (OUT / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "NOTICE").write_text("\n".join(["Sidq constraint-pair dataset", "", "SchemaStore/schemastore is included at the commit recorded in ATTRIBUTION.md under Apache-2.0.  Per-pair raw URLs, paths, commits, and licences are retained in the JSONL.", "", "Google Discovery JSON is not included: its payload redistribution licence could not be confirmed from the Discovery API terms."]) + "\n", encoding="utf-8")


def write_datasheet(progress: dict[str, Any], train: list[dict[str, Any]], evaluation: list[dict[str, Any]]) -> None:
    state = progress["schemastore"]; stats = state["stats"]; rows = train + evaluation; emitted = len(load_mined())
    source_rows = collections.Counter(row["source_kind"] for row in rows); sources = ("dbt", "discovery", "schemastore")
    lines = ["# Datasheet: constraint-pair corpus (v4)", "", "## Licence gate", "", "Google Discovery JSON was reviewed first and excluded. The [Discovery Service terms](https://developers.google.com/discovery/terms) say API use is governed by the Google APIs Terms; the CC-BY 4.0 notice is for the terms page, not a confirmed permissive licence for Discovery response payloads. SchemaStore/schemastore was admitted only after its pinned archive root `LICENSE` verified as Apache-2.0.", "", "## Composition", "", f"The rebuilt release has {len(rows)} rows ({len(train)} train / {len(evaluation)} eval), deterministic seed `{SEED}`, and a global identical-sentence cap of three. Distinct sentences: {len({sentence_key(row) for row in rows})}. Existing dbt v3 split membership is preserved; SchemaStore is held out by whole schema document (`source_document`), never by node.", "", "## Per-source survival", "", "| Source | Constrained nodes | No human description | With description | Rejected: prose does not express constraint | Survived nodes | Survival of described nodes | Released after cap |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |", "| dbt | — | — | — | — | — | — | " + str(source_rows["dbt"]) + " |", "| discovery | 0 | 0 | 0 | 0 | 0 | excluded (licence unconfirmed) | 0 |", f"| schemastore | {stats['constrained_nodes']} | {stats['no_description']} | {stats['with_description']} | {stats['not_expressed']} | {stats['survived_nodes']} | {stats['survived_nodes'] / stats['with_description']:.2%} | {source_rows['schemastore']} |" if stats["with_description"] else "| schemastore | 0 | 0 | 0 | 0 | 0 | n/a | 0 |", "", f"SchemaStore emitted {emitted} native-sentence rows from its {stats['survived_nodes']} survived nodes before the global cap; the cap selected {source_rows['schemastore']} rows. `No human description` is counted per constrained node and includes nodes for which only adjacent schema syntax exists. Complex-valued enums are separately excluded as unsupported executable values: {stats['unsupported_enum']}.", "", "## Released counts by source kind", "", "| Source kind | Rows | not_null | unique | accepted_values | relationships | expression | no_claim |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for source in sources:
        counts = type_counts(row for row in rows if row["source_kind"] == source)
        lines.append(f"| {source} | {source_rows[source]} | {counts['not_null']} | {counts['unique']} | {counts['accepted_values']} | {counts['relationships']} | {counts['expression']} | {counts['no_claim']} |")
    totals = type_counts(rows)
    lines.extend(["", "## Floor status", "", "| Type | Released | Floor | Status |", "| --- | ---: | ---: | --- |"])
    for kind in KINDS:
        floor = RARE_FLOORS.get(kind, 0); status = "under-sampled" if floor and totals[kind] < floor else "met / no floor"
        lines.append(f"| {kind} | {totals[kind]} | {floor or '—'} | {status} |")
    lines.extend(["", "## Filter decision", "", "A positive survives only when one native `description` sentence states its mapped constraint. Adjacency alone is rejected. Enum value glosses are not concatenated and do not count as an accepted-values statement: they explain individual values but do not natively assert that the complete enum is the allowed set. No sentence or pair is synthesized.", "", "## Checkpoints", "", "SchemaStore is checkpointed every 100 source documents in `data/claims/schema-corpora/`; each completed batch has a JSONL and manifest before progress advances. The intended production invocation is detached via `systemd-run --user`."])
    (OUT / "DATASHEET.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def rebuild(progress: dict[str, Any]) -> None:
    preserve_v3()
    mined = load_mined(); existing_train, existing_eval = legacy_rows("train"), legacy_rows("eval")
    combined: list[tuple[str, dict[str, Any]]] = [("train", row) for row in existing_train] + [("eval", row) for row in existing_eval]
    combined.extend((schema_split(row["source_document"]), row) for row in mined)
    capped = cap_rows(combined)
    train = sorted((row for split, row in capped if split == "train"), key=lambda row: stable_hash(json.dumps(row, sort_keys=True, ensure_ascii=False)))
    evaluation = sorted((row for split, row in capped if split == "eval"), key=lambda row: stable_hash(json.dumps(row, sort_keys=True, ensure_ascii=False)))
    write_jsonl(OUT / "train.jsonl", train); write_jsonl(OUT / "eval.jsonl", evaluation); write_schema(); write_attribution(progress, train, evaluation); write_datasheet(progress, train, evaluation)
    print(json.dumps({"train": len(train), "eval": len(evaluation), "by_type": type_counts(train + evaluation), "under_sampled": {kind: max(0, floor - type_counts(train + evaluation)[kind]) for kind, floor in RARE_FLOORS.items()}}, default=dict, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="resumable, licence-gated schema-description corpus miner")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--rebuild-only", action="store_true")
    args = parser.parse_args(); OUT.mkdir(parents=True, exist_ok=True); CHECKPOINTS.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        run_self_test(); return 0
    if PROGRESS.exists():
        progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
    else:
        commit = source_commit(); archive = fetch_schemastore(commit); _, paths = schema_paths(archive); progress = initial_progress(commit, paths); atomic_json(PROGRESS, progress)
    if not args.rebuild_only:
        collect_schemastore(progress)
    rebuild(progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
