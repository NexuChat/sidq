#!/usr/bin/env python3
"""Resumable, licence-gated miner for standards corpora (raw-v4 only).

The collector deliberately retains only descriptions that state a constraint.
It never turns a nearby definition into an assertion.  Each completed source
is an atomic checkpoint and can be resumed safely after a service restart.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import re
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

RAW = Path("data/claims/raw-v4")
PROGRESS = RAW / "_progress.json"
SEED = "sidq-standards-v4-20260729"
CAP_PER_SENTENCE = 3
KINDS = ("accepted_values", "relationships", "expression")
SPACE = re.compile(r"\s+")
SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"`])")
ASSERTION = re.compile(r"\b(?:must|shall|required|only|either|at least|at most|no more than|no less than|one of|valid values?|allowed values?|cannot|may not|between|matches?|pattern|minimum|maximum)\b", re.IGNORECASE)

# Sources whose published terms permit inclusion in an Apache-2.0 dataset.
# CC0 and MIT are Apache-compatible; the exact upstream licence remains in
# every row.  THO artefacts carrying an explicit third-party copyright are
# rejected before they can yield a row.
SOURCES = (
    {"id": "fhir_r5", "kind": "fhir_r5", "url": "https://packages.fhir.org/hl7.fhir.r5.core/5.0.0", "version": "5.0.0", "licence": "CC0-1.0", "decision": "include: FHIR R5 specification is CC0-1.0; third-party artefacts are rejected"},
    {"id": "tho", "kind": "tho", "url": "https://packages.fhir.org/hl7.terminology.r5/6.5.0", "version": "6.5.0", "licence": "CC0-1.0", "decision": "include only artefacts without a third-party copyright notice"},
    {"id": "niem", "kind": "excluded", "url": "https://niem.github.io/", "version": "unresolved", "licence": "NOASSERTION", "decision": "exclude: no source-level Apache-compatible redistribution grant was verified for the candidate model release"},
    {"id": "schemapile_perm", "kind": "excluded", "url": "https://huggingface.co/datasets/SchemaPile/SchemaPile", "version": "unresolved", "licence": "NOASSERTION", "decision": "exclude: corpus-level permissive slice and per-schema comment licences were not independently verifiable"},
    {"id": "aws_smithy", "kind": "smithy", "repo": "aws/aws-parallelcluster", "branch": "develop", "url": "https://github.com/aws/aws-parallelcluster", "version": "develop", "licence": "Apache-2.0", "decision": "include after archive LICENSE verification; report parsed Smithy node count"},
    {"id": "data_contract_cli", "kind": "datacontract", "repo": "datacontract/datacontract-cli", "url": "https://github.com/datacontract/datacontract-cli", "version": "main", "licence": "MIT", "decision": "include after archive LICENSE verification"},
)


def request(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sidq-standards-miner/4.0"})
    with urllib.request.urlopen(req, timeout=240) as response:
        return response.read()


def atomic(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temp.replace(path)


def clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = SPACE.sub(" ", value).strip()
    return value if len(value) >= 8 else None


def split_sentences(value: str) -> list[str]:
    return [part.strip() for part in SENTENCES.split(value) if len(part.strip()) >= 8] or [value]


def literal(value: Any, sentence: str) -> bool:
    text = str(value).lower() if isinstance(value, bool) else str(value)
    return bool(text and re.search(rf"(?<![\w-]){re.escape(text)}(?![\w-])", sentence, re.IGNORECASE))


def values_asserted(sentence: str, values: list[Any]) -> bool:
    return bool(values and len(values) <= 12 and ASSERTION.search(sentence) and all(literal(value, sentence) for value in values))


def expression_asserted(sentence: str) -> bool:
    return bool(ASSERTION.search(sentence))


def relationship_asserted(sentence: str, targets: list[str]) -> bool:
    return bool(re.search(r"\b(?:reference|refer|link|points? to|foreign key)\b", sentence, re.IGNORECASE) and any(literal(target, sentence) for target in targets))


def source_meta(source: dict[str, Any], path: str, version: str | None = None) -> dict[str, str]:
    return {"url": source["url"], "path": path, "version": version or source["version"], "licence": source["licence"]}


def make_row(source: dict[str, Any], document: str, column: str, kind: str, claim: dict[str, Any], sentence: str, version: str | None = None) -> dict[str, Any]:
    stable = hashlib.sha256((SEED + "\0" + source["id"] + "\0" + document).encode()).digest()[0]
    return {
        "class": "positive",
        "input": {"sentence": sentence, "column_name": column, "table_name": document, "schema_context": "standards-corpus"},
        "target": {"claim": claim},
        "source_kind": source["id"],
        "source_document": document,
        "source": source_meta(source, document, version),
        "split": "eval" if stable < 25 else "train",
        "seed": SEED,
    }


def fhir_valueset_codes(value_set: dict[str, Any]) -> list[str]:
    concepts: list[str] = []
    for include in (value_set.get("compose") or {}).get("include", []):
        if not isinstance(include, dict):
            continue
        for concept in include.get("concept", []):
            code = concept.get("code") if isinstance(concept, dict) else None
            if isinstance(code, str):
                concepts.append(code)
    return list(dict.fromkeys(concepts))


def fhir_rows(source: dict[str, Any], payload: bytes, terminology: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []; stats = {"nodes": 0, "by_type": collections.Counter(), "third_party_excluded": 0}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        docs: dict[str, dict[str, Any]] = {}
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".json") or not member.name.startswith("package/"):
                continue
            try:
                obj = json.loads(archive.extractfile(member).read())
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(obj, dict): docs[member.name.split("package/", 1)[1]] = obj
        # Explicit copyright in THO is the conservative third-party IP gate.
        for path, obj in docs.items():
            if terminology and clean(obj.get("copyright")):
                stats["third_party_excluded"] += 1; continue
            kind = obj.get("resourceType")
            if kind == "ValueSet":
                codes = fhir_valueset_codes(obj); description = clean(obj.get("description"))
                stats["nodes"] += 1
                if description:
                    for sentence in split_sentences(description):
                        if values_asserted(sentence, codes):
                            rows.append(make_row(source, path, obj.get("name") or path, "accepted_values", {"type": "accepted_values", "column": obj.get("name") or path, "values": codes}, sentence)); stats["by_type"]["accepted_values"] += 1
            if not terminology and kind == "StructureDefinition":
                for element in (obj.get("snapshot") or {}).get("element", []):
                    if not isinstance(element, dict): continue
                    column = element.get("path") or element.get("id") or path
                    for constraint in element.get("constraint", []):
                        if not isinstance(constraint, dict): continue
                        human, expr = clean(constraint.get("human")), clean(constraint.get("expression")); stats["nodes"] += 1
                        if human and expr:
                            for sentence in split_sentences(human):
                                if expression_asserted(sentence):
                                    rows.append(make_row(source, path, column, "expression", {"type": "expression", "column": column, "expr": expr}, sentence)); stats["by_type"]["expression"] += 1
                    profiles = []
                    for typ in element.get("type", []):
                        for target in typ.get("targetProfile", []) if isinstance(typ, dict) else []:
                            if isinstance(target, str): profiles.append(target.rsplit("/", 1)[-1])
                    if profiles:
                        stats["nodes"] += 1
                        for description in (clean(element.get("definition")), clean(element.get("comment")), clean(element.get("requirements"))):
                            if description:
                                for sentence in split_sentences(description):
                                    if relationship_asserted(sentence, profiles):
                                        rows.append(make_row(source, path, column, "relationships", {"type": "relationships", "column": column, "expr": "targetProfile=" + ",".join(profiles)}, sentence)); stats["by_type"]["relationships"] += 1
                    binding = element.get("binding")
                    if isinstance(binding, dict) and isinstance(binding.get("valueSet"), str):
                        target = binding["valueSet"].rsplit("/", 1)[-1]
                        match = next((item for item in docs.values() if item.get("resourceType") == "ValueSet" and item.get("url", "").endswith("/" + target)), None)
                        codes = fhir_valueset_codes(match or {})
                        stats["nodes"] += 1
                        for description in (clean(element.get("definition")), clean(element.get("comment")), clean(element.get("requirements"))):
                            if description:
                                for sentence in split_sentences(description):
                                    if values_asserted(sentence, codes):
                                        rows.append(make_row(source, path, column, "accepted_values", {"type": "accepted_values", "column": column, "values": codes}, sentence)); stats["by_type"]["accepted_values"] += 1
    return rows, stats


def licensed_archive(source: dict[str, Any]) -> tuple[zipfile.ZipFile, str]:
    repo = source["repo"]
    data = None
    branch = None
    for candidate in (source.get("branch"), "main", "master", "develop", "release", "v3"):
        if not candidate: continue
        try:
            data = request(f"https://codeload.github.com/{repo}/zip/{candidate}"); branch = candidate; break
        except urllib.error.HTTPError as error:
            if error.code != 404: raise
    if data is None or branch is None:
        raise ValueError(f"could not resolve an archive branch for {repo}")
    archive = zipfile.ZipFile(io.BytesIO(data)); root = archive.namelist()[0].split("/", 1)[0]
    licence = next((name for name in archive.namelist() if name.startswith(root + "/") and "/" not in name[len(root) + 1:] and Path(name).name.upper().startswith(("LICENSE", "COPYING"))), None)
    if not licence:
        raise ValueError(f"{repo} lacks a root LICENSE")
    text = archive.read(licence).decode("utf-8", errors="replace").lower()
    required = "apache license" if source["licence"] == "Apache-2.0" else "mit license"
    if required not in text:
        raise ValueError(f"{repo} license did not verify as {source['licence']}")
    source["version"] = branch
    return archive, root


def smithy_rows(source: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    archive, root = licensed_archive(source); rows: list[dict[str, Any]] = []; stats = {"nodes": 0, "documents": 0, "by_type": collections.Counter()}
    # This intentionally recognizes only literal Smithy traits, preserving the
    # documentation string as written.  Trait adjacency is not accepted.
    trait = re.compile(r'@documentation\("((?:[^"\\]|\\.)*)"\)\s*\n(?:(@[\w#.]+(?:\([^\n]*\))?\s*\n)*)\s*(\w+)\s*(?::\s*([\w#.]+))?', re.MULTILINE)
    for name in archive.namelist():
        if not name.endswith(".smithy") or "/test" in name.lower(): continue
        text = archive.read(name).decode("utf-8", errors="replace"); stats["documents"] += 1
        for match in trait.finditer(text):
            sentence_text = bytes(match.group(1), "utf-8").decode("unicode_escape"); traits = match.group(2) or ""; member = match.group(3); target = match.group(4) or ""
            stats["nodes"] += 1
            for sentence in split_sentences(SPACE.sub(" ", sentence_text)):
                range_m = re.search(r'@(range|length)\(([^)]*)\)', traits)
                pattern_m = re.search(r'@pattern\("([^"]+)"\)', traits)
                if (range_m or pattern_m) and expression_asserted(sentence):
                    expr = range_m.group(0) if range_m else pattern_m.group(0)
                    rows.append(make_row(source, name.split(root + "/", 1)[-1], member, "expression", {"type": "expression", "column": member, "expr": expr}, sentence)); stats["by_type"]["expression"] += 1
                # A member whose prose explicitly identifies the referenced
                # shape is a relationship.  A type alone is never sufficient.
                short = target.rsplit(".", 1)[-1]
                if target and relationship_asserted(sentence, [short]):
                    rows.append(make_row(source, name.split(root + "/", 1)[-1], member, "relationships", {"type": "relationships", "column": member, "expr": "target=" + target}, sentence)); stats["by_type"]["relationships"] += 1
    return rows, stats


def walk_contract(node: Any, document: str, source: dict[str, Any], parent: str = "root") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(node, dict):
        description = clean(node.get("description"))
        name = str(node.get("name") or parent)
        values = node.get("enum") or node.get("acceptedValues")
        if isinstance(values, list) and description:
            values = [value for value in values if isinstance(value, (str, int, float, bool))]
            for sentence in split_sentences(description):
                if values_asserted(sentence, values): rows.append(make_row(source, document, name, "accepted_values", {"type": "accepted_values", "column": name, "values": values}, sentence))
        for key in ("minimum", "maximum", "minLength", "maxLength", "pattern"):
            if key in node and description:
                for sentence in split_sentences(description):
                    if expression_asserted(sentence) and literal(node[key], sentence): rows.append(make_row(source, document, name, "expression", {"type": "expression", "column": name, "expr": f"{key}={node[key]}"}, sentence))
        for key, value in node.items():
            if isinstance(value, (dict, list)): rows.extend(walk_contract(value, document, source, name if key not in {"properties", "fields"} else parent))
    elif isinstance(node, list):
        for value in node: rows.extend(walk_contract(value, document, source, parent))
    return rows


def datacontract_rows(source: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    archive, root = licensed_archive(source); rows: list[dict[str, Any]] = []; docs = 0
    for name in archive.namelist():
        low = name.lower()
        if not (low.endswith((".yaml", ".yml", ".json")) and ("example" in low or "test" in low or "fixture" in low)): continue
        try:
            data = yaml.safe_load(archive.read(name)) if low.endswith((".yaml", ".yml")) else json.loads(archive.read(name))
        except (yaml.YAMLError, json.JSONDecodeError, UnicodeDecodeError): continue
        docs += 1; rows.extend(walk_contract(data, name.split(root + "/", 1)[-1], source))
    return rows, {"nodes": docs, "documents": docs, "by_type": collections.Counter(row["target"]["claim"]["type"] for row in rows)}


def cap_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used: collections.Counter[str] = collections.Counter(); kept = []
    # stable ordering means cap results are reproducible independent of timing.
    for row in sorted(rows, key=lambda item: (item["input"]["sentence"].casefold(), item["source_document"], item["target"]["claim"]["type"])):
        sentence = SPACE.sub(" ", row["input"]["sentence"]).casefold()
        if used[sentence] < CAP_PER_SENTENCE:
            kept.append(row); used[sentence] += 1
    return kept


def serial_stats(rows: list[dict[str, Any]], detail: dict[str, Any]) -> dict[str, Any]:
    counts = collections.Counter(row["target"]["claim"]["type"] for row in rows)
    distinct = {kind: len({row["input"]["sentence"].casefold() for row in rows if row["target"]["claim"]["type"] == kind}) for kind in KINDS}
    return {"rows": len(rows), "distinct_sentences": len({row["input"]["sentence"].casefold() for row in rows}), "by_type": {kind: {"rows": counts[kind], "distinct_sentences": distinct[kind]} for kind in KINDS}, "measured_nodes": detail.get("nodes", 0), "documents": detail.get("documents", 0), "third_party_excluded": detail.get("third_party_excluded", 0)}


def run_source(source: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if source["kind"] == "excluded": return [], {"nodes": 0, "documents": 0}
    if source["kind"] == "fhir_r5": return fhir_rows(source, request(source["url"]), False)
    if source["kind"] == "tho": return fhir_rows(source, request(source["url"]), True)
    if source["kind"] == "smithy": return smithy_rows(source)
    if source["kind"] == "datacontract": return datacontract_rows(source)
    raise ValueError(source["kind"])


def self_test() -> None:
    assert values_asserted("Must be one of planned, arrived, or finished.", ["planned", "arrived", "finished"])
    assert not values_asserted("Status of the encounter.", ["planned", "arrived"])
    assert expression_asserted("The value must be at least 2.")
    assert not expression_asserted("The retry setting.")
    assert relationship_asserted("A reference to Patient is required.", ["Patient"])
    assert not relationship_asserted("The subject of this event.", ["Patient"])


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--self-test", action="store_true"); parser.add_argument("--resume", action="store_true"); parser.add_argument("--restart", action="store_true", help="replace only this miner's selected generated checkpoints"); parser.add_argument("--source", choices=[source["id"] for source in SOURCES]); args = parser.parse_args()
    self_test()
    if args.self_test: print("self-test passed"); return
    RAW.mkdir(parents=True, exist_ok=True)
    if args.restart:
        for source in SOURCES:
            if args.source and source["id"] != args.source: continue
            for path in (RAW / f"{source['id']}.jsonl", RAW / f"{source['id']}.manifest.json"):
                if path.exists(): path.unlink()
        if PROGRESS.exists(): PROGRESS.unlink()
    progress = json.loads(PROGRESS.read_text(encoding="utf-8")) if args.resume and PROGRESS.exists() else {"seed": SEED, "sources": {}}
    selected = [source for source in SOURCES if not args.source or source["id"] == args.source]
    for source in selected:
        source_id = source["id"]
        if progress["sources"].get(source_id, {}).get("status") == "complete": continue
        started = time.time()
        try:
            rows, detail = run_source(source)
        except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile, tarfile.TarError) as error:
            # A source that cannot pass its gate is auditable as excluded and
            # does not prevent already-checkpointed sources from resuming.
            rows, detail = [], {"nodes": 0, "documents": 0, "error": str(error)}
            source = {**source, "decision": source["decision"] + "; runtime exclusion: " + str(error)}
        rows = cap_rows(rows)
        output = RAW / f"{source_id}.jsonl"; write_jsonl(output, rows)
        summary = {"source": {key: value for key, value in source.items() if key != "repo"}, "status": "complete", "elapsed_seconds": round(time.time() - started, 2), **serial_stats(rows, detail)}
        if detail.get("error"): summary["error"] = detail["error"]
        atomic(RAW / f"{source_id}.manifest.json", summary); progress["sources"][source_id] = summary; atomic(PROGRESS, progress)
        print(json.dumps({source_id: summary["by_type"], "nodes": summary["measured_nodes"]}, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
