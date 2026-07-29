#!/usr/bin/env python3
"""Resumable, licence-audited multi-source constraint-pair collector.

Every batch is durable before progress advances.  It is safe to kill this
process between batches: ``--resume`` reconciles manifests then continues at
the next unfinished twenty-repository batch.  Raw checkpoints intentionally
retain unfiltered candidates so source-level survival can be audited later.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

OUT = Path("data/claims")
RAW = OUT / "raw-v3"
PROGRESS = RAW / "_progress.json"
LOCK = RAW / ".mine.lock"
SCHEMA_VERSION = "3.0.0"
SPLIT_SEED = "sidq-multisource-claims-v3-20260729"
BATCH_SIZE = 20
TARGET_ROWS = 20_000
EVAL_TARGET_ROWS = 4_000
EVAL_MIN_PER_TYPE = 300
PREDICATE_TYPES = ("not_null", "unique", "accepted_values", "relationships", "expression")
RARE_TYPE_FLOORS = {"accepted_values": 1_500, "relationships": 1_500, "expression": 1_500}
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Unlicense", "CC0-1.0"}
SOURCE_NAMES = ("dbt_schema_yml", "sql_ddl", "great_expectations", "soda")
HARD_NEGATIVE_RE = re.compile(r"\b(?:source of truth|updated (?:regularly|daily|weekly|monthly)|important (?:for|to)|used (?:for|by)|powers?|supports?|enables?|contains? (?:information|data)|tracks?|represents?|stores?|provides?|central|canonical|primary)\b", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`])")
NOT_NULL_RE = re.compile(r"\b(?:not[ -]?null|never null|cannot be null|can't be null|required|mandatory|must not be null|must be present|always populated|must not be empty|non[ -]?empty)\b", re.IGNORECASE)
NOT_NULL_NEGATION_RE = re.compile(r"\b(?:not required|optional|may be null|can be null|nullable)\b", re.IGNORECASE)
UNIQUE_RE = re.compile(r"\b(?:unique|distinct|primary key|surrogate key|no duplicates?|one (?:row|record|value|entry) per)\b", re.IGNORECASE)
VALUES_RE = re.compile(r"\b(?:one of|only|allowed values?|valid values?|must (?:be|exist)|permitted values?)\b", re.IGNORECASE)
LOWER_RE = re.compile(r"(?:>=|≥|at least|minimum of|no less than|greater than or equal to)", re.IGNORECASE)
UPPER_RE = re.compile(r"(?:<=|≤|at most|maximum of|no more than|less than or equal to)", re.IGNORECASE)
ENUM_LIST_RE = re.compile(r"\b(?:one of|allowed values?|valid values?|permitted values?|must (?:be|exist))\b\s*(?:are|is)?\s*(?::|=)?\s*(.+)", re.IGNORECASE)
COMPARISON_RE = re.compile(r"(?P<operator>>=|<=|>|<)\s*(?P<number>-?\d+(?:\.\d+)?)")
MIN_CALL_RE = re.compile(r"\.(?:min|min_length)\(\s*(?P<number>-?\d+(?:\.\d+)?)")
MAX_CALL_RE = re.compile(r"\.(?:max|max_length)\(\s*(?P<number>-?\d+(?:\.\d+)?)")

DBT_SEEDS = ("dbt-labs/jaffle_shop", "dbt-labs/dbt-utils", "calogica/dbt-expectations", "Metaplane/dbt-expectations", "fivetran/dbt_shopify", "snowplow/dbt-snowplow-web")
DDL_SEEDS = ("liquibase/liquibase", "flyway/flyway", "hasura/graphql-engine", "supabase/supabase", "directus/directus", "prisma/prisma", "metabase/metabase", "appwrite/appwrite")
GE_SEEDS = ("great-expectations/great_expectations", "greatexpectations/great_expectations", "elementary-data/elementary", "datahub-project/datahub")
SODA_SEEDS = ("sodadata/soda-core", "sodadata/soda-spark", "sodadata/soda-sql", "elementary-data/elementary")
DISCOVERY = {
    "dbt_schema_yml": ("dbt in:name", "topic:dbt", "\"data build tool\" in:description", "dbt language:SQL", "dbt language:Python", "dbt stars:0..5", "dbt stars:6..25", "dbt stars:26..100", "dbt stars:>100"),
    "sql_ddl": ("\"database migrations\" in:description", "topic:database topic:sql", "\"CREATE TABLE\" in:readme"),
    "great_expectations": ("\"great expectations\" in:description", "great_expectations in:name"),
    "soda": ("\"soda checks\" in:description", "sodadata in:description"),
}
SEEDS = {"dbt_schema_yml": DBT_SEEDS, "sql_ddl": DDL_SEEDS, "great_expectations": GE_SEEDS, "soda": SODA_SEEDS}
SOURCEGRAPH_QUERIES = {
    "dbt_schema_yml": "(file:schema.yml OR file:schema.yaml) (accepted_values OR relationships OR expression_is_true OR not_null OR unique) count:10000",
    "sql_ddl": "file:\\.sql (COMMENT ON COLUMN OR --) (CHECK OR REFERENCES OR NOT NULL OR UNIQUE) count:10000",
    "great_expectations": "file:expectations/ expectation_type meta notes count:10000",
    "soda": "file:soda\\.ya?ml \"checks for\" count:10000",
}


@dataclass(frozen=True)
class RepoInfo:
    name: str
    commit: str
    licence: str
    branch: str


def request_json(url: str) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sidq-multisource-miner/3.0"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def discover(repo_count: int) -> list[dict[str, str]]:
    """Build a persisted, source-tagged plan; every result is licence-gated later."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    quotas = {"dbt_schema_yml": 2_200, "sql_ddl": 400, "great_expectations": 150, "soda": 150}
    for source in SOURCE_NAMES:
        source_found = 0
        for repo in SEEDS[source]:
            if repo not in seen:
                found.append({"repo": repo, "source": source}); seen.add(repo); source_found += 1
        for query in DISCOVERY[source]:
            for page in range(1, 11):
                if source_found >= quotas[source] or len(found) >= repo_count:
                    break
                url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
                try:
                    items = request_json(url).get("items", [])
                except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
                    print(f"discovery {source} stopped: {error}", file=sys.stderr); break
                if not items:
                    break
                for item in items:
                    repo = item.get("full_name") if isinstance(item, dict) else None
                    if isinstance(repo, str) and repo.count("/") == 1 and repo not in seen:
                        found.append({"repo": repo, "source": source}); seen.add(repo); source_found += 1
                        if source_found >= quotas[source] or len(found) >= repo_count:
                            break
    # Start each batch with a mix of sources, rather than postponing rare-type
    # collection until after the large dbt lane is exhausted.
    grouped = {source: [entry for entry in found if entry["source"] == source] for source in SOURCE_NAMES}
    interleaved: list[dict[str, str]] = []
    while any(grouped.values()):
        for source in SOURCE_NAMES:
            if grouped[source]: interleaved.append(grouped[source].pop(0))
    return interleaved[:repo_count]


def sourcegraph_candidates(source: str) -> list[str]:
    """Licence-untrusted discovery only; archive admission remains mandatory."""
    url = "https://sourcegraph.com/.api/search/stream?" + urllib.parse.urlencode({"q": f"context:global {SOURCEGRAPH_QUERIES[source]}", "v": "V3"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sidq-multisource-miner/3.0"}), timeout=120) as response:
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
        print(f"Sourcegraph {source} discovery unavailable: {error}", file=sys.stderr)
        return []
    return list(dict.fromkeys(re.findall(r'"repository":"github\\.com/([^"]+)"', body)))


def expand_plan(progress: dict[str, Any], limit: int) -> int:
    """Append unseen candidates without changing completed work or its order."""
    current = {entry["repo"] for entry in progress["planned_repos"]}
    candidates: list[dict[str, str]] = []
    legacy = OUT / "raw" / "_github_candidates.json"
    if legacy.exists():
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            candidates.extend({"repo": item["repo"], "source": "dbt_schema_yml"} for item in payload.get("candidates", []) if isinstance(item, dict) and isinstance(item.get("repo"), str))
        except (OSError, json.JSONDecodeError):
            pass
    for source in SOURCE_NAMES:
        candidates.extend({"repo": repo, "source": source} for repo in sourcegraph_candidates(source))
    added = 0
    for entry in candidates:
        if len(progress["planned_repos"]) >= limit:
            break
        if entry["repo"].count("/") == 1 and entry["repo"] not in current:
            progress["planned_repos"].append(entry); progress["repos_remaining"].append(entry); current.add(entry["repo"]); added += 1
    return added


def archive_licence(archive: zipfile.ZipFile) -> str | None:
    for member in archive.infolist():
        bits = member.filename.split("/", 1)
        if len(bits) != 2 or "/" in bits[1] or not bits[1].lower().startswith(("license", "copying", "unlicense")):
            continue
        text = archive.read(member).decode("utf-8", errors="replace").lower()
        if "apache license" in text and "version 2.0" in text: return "Apache-2.0"
        if "permission is hereby granted, free of charge" in text and "software" in text: return "MIT"
        if "redistribution and use in source and binary forms" in text: return "BSD-3-Clause" if "neither the name" in text else "BSD-2-Clause"
        if "this is free and unencumbered software released into the public domain" in text: return "Unlicense"
        if "creative commons zero" in text or "cc0 1.0 universal" in text: return "CC0-1.0"
    return None


def fetch_info(repo: str) -> RepoInfo | None:
    try:
        metadata = request_json(f"https://api.github.com/repos/{repo}")
        licence = (metadata.get("license") or {}).get("spdx_id")
        if licence not in ALLOWED_LICENSES:
            return None
        branch = metadata["default_branch"]
        commit = request_json(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch)}")["sha"]
        return RepoInfo(repo, commit, licence, branch)
    except urllib.error.HTTPError as error:
        # The unauthenticated metadata API is small; codeload remains usable
        # after it is exhausted and the archive comment carries the exact SHA.
        if error.code != 403:
            raise
    for branch in ("main", "master", "develop", "trunk"):
        try:
            headers = {"User-Agent": "sidq-multisource-miner/3.0"}
            with urllib.request.urlopen(urllib.request.Request(f"https://codeload.github.com/{repo}/zip/{branch}", headers=headers), timeout=180) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        commit = archive.comment.decode("ascii", errors="ignore")
        licence = archive_licence(archive)
        if re.fullmatch(r"[0-9a-f]{40}", commit) and licence in ALLOWED_LICENSES:
            return RepoInfo(repo, commit, licence, branch)
        return None
    return None


def fetch_archive(info: RepoInfo) -> zipfile.ZipFile:
    headers = {"User-Agent": "sidq-multisource-miner/3.0"}
    with urllib.request.urlopen(urllib.request.Request(f"https://codeload.github.com/{info.name}/zip/{info.commit}", headers=headers), timeout=180) as response:
        return zipfile.ZipFile(io.BytesIO(response.read()))


def notice_from(archive: zipfile.ZipFile) -> str | None:
    for member in archive.infolist():
        bits = member.filename.split("/", 1)
        if len(bits) == 2 and bits[1].upper() == "NOTICE":
            return archive.read(member).decode("utf-8", errors="replace").strip() or None
    return None


def clean_text(value: Any) -> str | None:
    if not isinstance(value, str): return None
    value = re.sub(r"\s+", " ", value).strip(" -\t\n")
    return value if len(value) >= 8 and not value.startswith("{{") else None


def sentence_list(value: str) -> list[str]:
    return [part.strip(" -\t\n") for part in SENTENCE_RE.split(value) if len(part.strip()) >= 8] or [value]


def record(sentence: str, table: str, column: str | None, context: str, info: RepoInfo, path: str, source: str, claim: dict[str, Any] | None) -> dict[str, Any]:
    return {"class": "positive" if claim else ("hard_negative" if HARD_NEGATIVE_RE.search(sentence) else "negative"), "input": {"sentence": sentence, "column_name": column, "table_name": table, "schema_context": context}, "target": {"claim": claim}, "source": {"repo": info.name, "path": path, "commit": info.commit, "licence": info.licence, "collection_source": source}}


def literals(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) and value and all(isinstance(x, (str, int, float, bool)) for x in value) else None


def dbt_claim(name: str, config: dict[str, Any], column: str | None) -> dict[str, Any] | None:
    args = config | (config.get("arguments") if isinstance(config.get("arguments"), dict) else {})
    short = name.rsplit(".", 1)[-1]
    if short in {"not_null", "unique"} and column: return {"type": short, "column": column}
    if short == "accepted_values" and column and (values := literals(args.get("values"))): return {"type": "accepted_values", "column": column, "values": values}
    if short == "relationships" and column and isinstance(args.get("to"), str) and isinstance(args.get("field"), str): return {"type": "relationships", "column": column, "expr": f"to={args['to']};field={args['field']}"}
    if short in {"expression_is_true", "accepted_range"} and column:
        expr = args.get("expression")
        if isinstance(expr, str) and expr.strip(): return {"type": "expression", "column": column, "expr": expr.strip()}
        bounds = [(key, label) for key, label in (("min_value", "min"), ("max_value", "max")) if key in args]
        if bounds and all(isinstance(args[k], (int, float)) for k, _ in bounds): return {"type": "expression", "column": column, "expr": "range(" + ";".join(f"{label}={args[key]}" for key, label in bounds) + ")"}
    return None


def tests(items: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    if not isinstance(items, list): return
    for item in items:
        if isinstance(item, str): yield item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, config = next(iter(item.items()))
            if isinstance(name, str): yield name, config if isinstance(config, dict) else {}


def mine_dbt(archive: zipfile.ZipFile, info: RepoInfo, root: str, dropped: collections.Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for full_path in archive.namelist():
        low = full_path.lower(); base = low.rsplit("/", 1)[-1]
        if not base.endswith((".yml", ".yaml")) or base in {"packages.yml", "dependencies.yml", "dbt_project.yml"} or "/target/" in low or "/dbt_packages/" in low:
            continue
        try: doc = yaml.safe_load(archive.read(full_path))
        except (yaml.YAMLError, UnicodeDecodeError, OSError): continue
        if not isinstance(doc, dict): continue
        path = full_path.removeprefix(root)
        for section in ("models", "sources", "seeds", "snapshots"):
            nodes = doc.get(section)
            if not isinstance(nodes, list): continue
            for node in nodes:
                resources = node.get("tables", []) if section == "sources" and isinstance(node, dict) and isinstance(node.get("tables"), list) else [node]
                for model in resources:
                    if not isinstance(model, dict) or not isinstance(model.get("name"), str): continue
                    table = model["name"]; desc = clean_text(model.get("description")); model_tests = list(tests(model.get("tests"))) + list(tests(model.get("data_tests")))
                    if desc and not model_tests:
                        rows.extend(record(s, table, None, "dbt schema.yml", info, path, "dbt_schema_yml", None) for s in sentence_list(desc))
                    for col in model.get("columns", []) if isinstance(model.get("columns"), list) else []:
                        if not isinstance(col, dict) or not isinstance(col.get("name"), str): continue
                        description = clean_text(col.get("description")); mapped = [dbt_claim(name, cfg, col["name"]) for name, cfg in list(tests(col.get("tests"))) + list(tests(col.get("data_tests")))]
                        mapped = [claim for claim in mapped if claim]
                        if description and mapped:
                            for s in sentence_list(description):
                                rows.extend(record(s, table, col["name"], "dbt schema.yml", info, path, "dbt_schema_yml", claim) for claim in mapped)
                        elif description and not mapped:
                            rows.extend(record(s, table, col["name"], "dbt schema.yml", info, path, "dbt_schema_yml", None) for s in sentence_list(description))
    return rows


def sql_comments(text: str) -> tuple[dict[tuple[str, str], str], dict[int, str]]:
    comment_on: dict[tuple[str, str], str] = {}
    adjacent: dict[int, str] = {}
    pending: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*--\s*(.+?)\s*$", line)
        if match:
            pending.append(match.group(1)); continue
        if line.strip():
            if pending: adjacent[number] = " ".join(pending); pending = []
            on = re.search(r"COMMENT\s+ON\s+COLUMN\s+([\w.\"`]+)\s+IS\s+'((?:''|[^'])*)'", line, re.IGNORECASE)
            if on:
                bits = [part.strip('"`') for part in on.group(1).split(".")]
                if len(bits) >= 2: comment_on[(bits[-2].casefold(), bits[-1].casefold())] = on.group(2).replace("''", "'")
    return comment_on, adjacent


def ddl_candidates(line: str, table: str | None) -> list[tuple[dict[str, Any], str | None, str | None]]:
    """Return parsed DDL constraints as (claim, table, column)."""
    result = []
    fk = re.search(r"(?:FOREIGN\s+KEY\s*\(\s*([\w\"`]+)\s*\)|([\w\"`]+))\s+REFERENCES\s+([\w.\"`]+)\s*\(\s*([\w\"`]+)\s*\)", line, re.IGNORECASE)
    if fk:
        col = (fk.group(1) or fk.group(2)).strip('"`'); target, target_col = fk.group(3).strip('"`'), fk.group(4).strip('"`')
        result.append(({"type": "relationships", "column": col, "expr": f"to={target};field={target_col}"}, table, col))
    match = re.match(r"\s*([\w\"`]+)\s+.+", line)
    col = match.group(1).strip('"`') if match and not re.match(r"\s*(?:CONSTRAINT|FOREIGN|PRIMARY|UNIQUE|CHECK)\b", line, re.IGNORECASE) else None
    if re.search(r"\bNOT\s+NULL\b", line, re.IGNORECASE) and col: result.append(({"type": "not_null", "column": col}, table, col))
    if re.search(r"\b(?:UNIQUE|PRIMARY\s+KEY)\b", line, re.IGNORECASE) and col: result.append(({"type": "unique", "column": col}, table, col))
    check = re.search(r"\bCHECK\s*\((.*)\)", line, re.IGNORECASE)
    if check and col:
        expr = check.group(1).strip()
        enum = re.search(r"\bIN\s*\((.*?)\)", expr, re.IGNORECASE)
        if enum:
            values = re.findall(r"'((?:''|[^'])*)'|\b(-?\d+(?:\.\d+)?)\b", enum.group(1))
            parsed: list[Any] = [a.replace("''", "'") if a else float(b) if "." in b else int(b) for a, b in values]
            if parsed: result.append(({"type": "accepted_values", "column": col, "values": parsed}, table, col))
        else: result.append(({"type": "expression", "column": col, "expr": expr}, table, col))
    return result


def mine_ddl(archive: zipfile.ZipFile, info: RepoInfo, root: str, stats: collections.Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for full_path in archive.namelist():
        lower = full_path.lower()
        if not lower.endswith(".sql") or any(part in lower for part in ("/node_modules/", "/vendor/", "/target/")): continue
        try: text = archive.read(full_path).decode("utf-8", errors="replace")
        except OSError: continue
        comments, adjacent = sql_comments(text); table: str | None = None
        path = full_path.removeprefix(root)
        for number, line in enumerate(text.splitlines(), 1):
            create = re.search(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`]+)", line, re.IGNORECASE)
            if create: table = create.group(1).split(".")[-1].strip('"`')
            for claim, claim_table, col in ddl_candidates(line, table):
                stats["constraints_found"] += 1
                prose = comments.get(((claim_table or "").casefold(), (col or "").casefold())) or adjacent.get(number)
                prose = clean_text(prose)
                if not prose:
                    stats["constraints_without_human_comment"] += 1; continue
                stats["constraints_with_human_comment"] += 1
                rows.extend(record(s, claim_table or "unknown", col, "SQL DDL or migration", info, path, "sql_ddl", claim) for s in sentence_list(prose))
    return rows


def expectation_claim(expectation: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str]:
    name = expectation.get("expectation_type")
    kwargs = expectation.get("kwargs", {}) if isinstance(expectation.get("kwargs"), dict) else {}
    col = kwargs.get("column") if isinstance(kwargs.get("column"), str) else None
    if name == "expect_column_values_to_not_be_null" and col: return {"type": "not_null", "column": col}, col, col
    if name == "expect_column_values_to_be_unique" and col: return {"type": "unique", "column": col}, col, col
    if name == "expect_column_values_to_be_in_set" and col and (values := literals(kwargs.get("value_set"))): return {"type": "accepted_values", "column": col, "values": values}, col, col
    if name in {"expect_column_values_to_be_between", "expect_column_values_to_be_greater_than", "expect_column_values_to_be_less_than"} and col:
        bits = []
        if isinstance(kwargs.get("min_value"), (int, float)): bits.append(f"min={kwargs['min_value']}")
        if isinstance(kwargs.get("max_value"), (int, float)): bits.append(f"max={kwargs['max_value']}")
        return ({"type": "expression", "column": col, "expr": "range(" + ";".join(bits) + ")"} if bits else None), col, col
    return None, col, col or "suite"


def notes(value: Any) -> list[str]:
    if isinstance(value, str): return sentence_list(value) if clean_text(value) else []
    if isinstance(value, list): return [s for x in value for s in notes(x)]
    if isinstance(value, dict): return [s for key in ("notes", "description", "name") for s in notes(value.get(key))]
    return []


def mine_ge(archive: zipfile.ZipFile, info: RepoInfo, root: str) -> list[dict[str, Any]]:
    rows = []
    for full_path in archive.namelist():
        if not full_path.lower().endswith(".json") or "expect" not in full_path.lower(): continue
        try: doc = json.loads(archive.read(full_path))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError): continue
        expectations = doc.get("expectations") if isinstance(doc, dict) else None
        if not isinstance(expectations, list): continue
        suite_notes = notes(doc.get("meta", {})); path = full_path.removeprefix(root)
        for expectation in expectations:
            if not isinstance(expectation, dict): continue
            claim, col, table = expectation_claim(expectation)
            if not claim: continue
            prose = notes(expectation.get("meta", {})) or suite_notes
            rows.extend(record(s, table, col, "Great Expectations suite", info, path, "great_expectations", claim) for s in prose)
    return rows


def soda_claim(text: str) -> tuple[dict[str, Any] | None, str | None]:
    column = re.search(r"\(([^)]+)\)", text)
    col = column.group(1).strip() if column else None
    if col and re.search(r"\bmissing_count\b", text, re.IGNORECASE): return {"type": "not_null", "column": col}, col
    if col and re.search(r"\bduplicate_count\b", text, re.IGNORECASE): return {"type": "unique", "column": col}, col
    enum = re.search(r"\bvalues?\s+in\s*\(([^)]+)\).*?\b(?:must|should)\b.*?\((.*?)\)", text, re.IGNORECASE)
    if enum:
        vals = [part.strip(" '\"") for part in enum.group(2).split(",") if part.strip(" '\"")]
        if vals: return {"type": "accepted_values", "column": enum.group(1).strip(), "values": vals}, enum.group(1).strip()
    return None, col


def mine_soda(archive: zipfile.ZipFile, info: RepoInfo, root: str) -> list[dict[str, Any]]:
    rows = []
    for full_path in archive.namelist():
        if not full_path.lower().endswith((".yml", ".yaml")) or "soda" not in full_path.lower(): continue
        try: doc = yaml.safe_load(archive.read(full_path))
        except (yaml.YAMLError, UnicodeDecodeError, OSError): continue
        if not isinstance(doc, dict): continue
        path = full_path.removeprefix(root)
        for table_key, checks in doc.items():
            if not isinstance(table_key, str) or not table_key.lower().startswith("checks for") or not isinstance(checks, list): continue
            table = table_key.split(" for ", 1)[-1].strip()
            for check in checks:
                phrase, config = (next(iter(check.items())) if isinstance(check, dict) and len(check) == 1 else (check, {}))
                if not isinstance(phrase, str): continue
                claim, col = soda_claim(phrase)
                if not claim: continue
                prose = notes(config) if isinstance(config, dict) else []
                # A Soda check expression is accepted only when it already reads as an expectation.
                if not prose and re.search(r"\b(?:must|should|is one of|allowed)\b", phrase, re.IGNORECASE): prose = sentence_list(phrase)
                rows.extend(record(s, table, col, "Soda check", info, path, "soda", claim) for s in prose)
    return rows


def normal(value: str) -> str: return re.sub(r"\s+", " ", value.casefold().replace("`", "")).strip()

def literal_in(value: Any, sentence: str) -> bool:
    text = str(value).casefold() if not isinstance(value, bool) else str(value).lower()
    # A full-stop is normal sentence punctuation, not part of an identifier.
    return bool(text and re.search(rf"(?<![\w-]){re.escape(text)}(?![\w-])", sentence, re.IGNORECASE))


def sentence_values(values: list[Any], text: str) -> list[Any] | None:
    """Return the enum subset stated by prose, or ``None`` for a mismatch."""
    mentioned = [value for value in values if literal_in(value, text)]
    if not mentioned or not VALUES_RE.search(text):
        return None
    # When prose gives an explicit list, every list member must be a code enum
    # member.  Otherwise an adjacent assertion could silently relabel a value
    # the sentence never licensed us to train on.
    if match := ENUM_LIST_RE.search(text):
        pieces = re.split(r"\s*(?:,|;|/|\||\band\b|\bor\b)\s*", match.group(1).strip(" ."), flags=re.IGNORECASE)
        for piece in pieces:
            item = piece.strip(" '\"`[](){}.")
            if item and not any(literal_in(value, item) for value in values):
                return None
    return mentioned


def numeric_bound(expr: str) -> tuple[str, str] | None:
    """Map a single explicit numeric expression to its comparison direction."""
    comparisons = COMPARISON_RE.findall(expr)
    if len(comparisons) == 1:
        return comparisons[0]
    if match := MIN_CALL_RE.search(expr):
        return ">=", match.group("number")
    if match := MAX_CALL_RE.search(expr):
        return "<=", match.group("number")
    return None


def stated_bound(text: str, number: str) -> str | None:
    """Return the exact inequality the sentence states for ``number``."""
    patterns = (
        (">=", r"(?:at least|minimum of|no less than|greater than or equal to)\s*"),
        ("<=", r"(?:at most|maximum of|no more than|less than or equal to)\s*"),
        (">", r"(?:more than|greater than)\s*"),
        ("<", r"(?:less than|fewer than|under)\s*"),
    )
    for operator, prefix in patterns:
        if re.search(prefix + re.escape(number) + r"(?![\w.-])", text, re.IGNORECASE):
            return operator
    return None


def semantic_expression(text: str, expr: str, bound: tuple[str, str] | None) -> bool:
    """Recognise only English phrases with one unambiguous executable bound."""
    if bound:
        operator, number = bound
        if number in {"0", "0.0"}:
            if operator == ">" and re.search(r"(?<!non[ -])\bpositive\b", text):
                return True
            if operator == ">=" and re.search(r"\bnon[ -]?negative\b", text):
                return True
            if operator == "<" and re.search(r"(?<!non[ -])\bnegative\b", text):
                return True
            if operator == "<=" and re.search(r"\bnon[ -]?positive\b", text):
                return True
            if operator == ">" and re.search(r"\b(?:non[ -]?empty|not (?:be )?empty|not (?:be )?blank)\b", text) and re.search(r"\b(?:len(?:gth)?|trim)\b", expr, re.IGNORECASE):
                return True
        if stated_bound(text, number) == operator:
            return True
    if re.search(r"\b(?:current_date|current_timestamp|now\s*\(|today\s*\()", expr, re.IGNORECASE):
        if ">" in expr and re.search(r"\b(?:in the future|future)\b", text, re.IGNORECASE):
            return True
        if "<" in expr and re.search(r"\b(?:in the past|past)\b", text, re.IGNORECASE):
            return True
    return False

def expressed(row: dict[str, Any]) -> bool:
    claim = row["target"]["claim"]
    if not claim: return True
    text = normal(row["input"]["sentence"]); kind = claim["type"]
    if kind == "not_null": return bool((NOT_NULL_RE.search(text) and not NOT_NULL_NEGATION_RE.search(text)) or re.search(r"\bprimary key\b", text))
    if kind == "unique": return bool(UNIQUE_RE.search(text))
    if kind == "accepted_values":
        values = claim.get("values", [])
        if not isinstance(values, list) or (stated := sentence_values(values, text)) is None:
            return False
        claim["values"] = stated
        return True
    if kind == "relationships":
        names = re.findall(r"['\"]([^'\"]+)['\"]", claim.get("expr", "")); field = re.search(r"field=([^;]+)", claim.get("expr", ""))
        return bool(names and field and re.search(r"\b(?:references?|refers? to|foreign key|links? to)\b", text) and names[-1].casefold() in text and field.group(1).strip(" '\"").casefold() in text)
    if kind == "expression":
        expr = claim.get("expr", "")
        nums = re.findall(r"-?\d+(?:\.\d+)?", expr)
        if semantic_expression(text, expr, numeric_bound(expr)):
            return True
        if nums: return bool(all(literal_in(number, text) for number in nums) and (LOWER_RE.search(text) or UPPER_RE.search(text) or re.search(r"\b(?:between|range|within)\b", text)))
        return bool(re.search(r"\b(?:must|should|at least|at most|greater than|less than)\b", text))
    return False


def ptype(row: dict[str, Any]) -> str: return row["target"]["claim"]["type"] if row["target"]["claim"] else "no-claim"
def source_name(row: dict[str, Any]) -> str: return row["source"]["collection_source"]
def row_hash(row: dict[str, Any]) -> str: return hashlib.sha256((SPLIT_SEED + json.dumps(row, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
def repo_hash(repo: str) -> str: return hashlib.sha256((SPLIT_SEED + repo).encode()).hexdigest()

def filter_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, collections.Counter[str]]]:
    keep = []; stats: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in rows:
        source = source_name(row)
        if row["class"] == "positive":
            stats[source]["positive_candidates"] += 1
            if expressed(row): keep.append(row); stats[source]["positive_survived"] += 1
            else: stats[source]["positive_rejected_not_expressed"] += 1
        else: keep.append(row); stats[source]["non_positive"] += 1
    return keep, stats


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        for row in rows: out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)

def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)

def initial_progress(plan: list[dict[str, str]]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "batch_size": BATCH_SIZE, "seed": SPLIT_SEED, "planned_repos": plan, "completed_repos": [], "repos_done": 0, "repos_remaining": plan.copy(), "completed_batches": [], "class_counts": {}, "per_type_counts": {kind: 0 for kind in PREDICATE_TYPES}, "source_stats": {source: {} for source in SOURCE_NAMES}, "sources": {}, "skipped": []}

def reconcile(progress: dict[str, Any]) -> dict[str, Any]:
    for path in sorted(RAW.glob("batch-*.manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["batch"] not in progress["completed_batches"]: apply_manifest(progress, manifest)
    atomic_json(PROGRESS, progress); return progress

def apply_manifest(progress: dict[str, Any], manifest: dict[str, Any]) -> None:
    progress["completed_batches"].append(manifest["batch"]); progress["completed_repos"].extend(manifest["repos"])
    done = {entry["repo"] for entry in progress["completed_repos"]}; progress["repos_done"] = len(done); progress["repos_remaining"] = [entry for entry in progress["planned_repos"] if entry["repo"] not in done]
    for key in ("class_counts", "per_type_counts"):
        counter = collections.Counter(progress.get(key, {})); counter.update(manifest.get(key, {})); progress[key] = dict(counter)
    for source, values in manifest.get("source_stats", {}).items():
        counter = collections.Counter(progress.setdefault("source_stats", {}).get(source, {})); counter.update(values); progress["source_stats"][source] = dict(counter)
    progress["sources"].update(manifest.get("sources", {})); progress["skipped"] = sorted(set(progress.get("skipped", [])) | set(manifest.get("skipped", [])))

def lock() -> None:
    try: descriptor = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error: raise RuntimeError("collection already running; remove only a confirmed stale raw-v3/.mine.lock") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as out: json.dump({"pid": os.getpid(), "started_at": int(time.time())}, out)

def unlock() -> None: LOCK.unlink(missing_ok=True)

def mine_one(entry: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any], collections.Counter[str]]:
    info = fetch_info(entry["repo"])
    if info is None: return [], {}, collections.Counter({"skipped_license": 1})
    archive = fetch_archive(info); actual = archive_licence(archive)
    if actual not in ALLOWED_LICENSES: return [], {}, collections.Counter({"skipped_archive_license": 1})
    info = RepoInfo(info.name, info.commit, actual, info.branch); root = archive.namelist()[0].split("/", 1)[0] + "/"; ddl = collections.Counter()
    source = entry["source"]
    rows = {"dbt_schema_yml": mine_dbt, "sql_ddl": mine_ddl, "great_expectations": mine_ge, "soda": mine_soda}[source](archive, info, root, ddl) if source in {"dbt_schema_yml", "sql_ddl"} else {"great_expectations": mine_ge, "soda": mine_soda}[source](archive, info, root)
    source_meta = {"commit": info.commit, "licence": info.licence, "notice": notice_from(archive), "collection_sources": [source]}
    return rows, source_meta, ddl

def collect_batch(entries: list[dict[str, str]], number: int, pause: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []; sources: dict[str, Any] = {}; skipped: list[str] = []; ddl = collections.Counter()
    for entry in entries:
        try:
            mined, meta, ddl_stats = mine_one(entry); ddl.update(ddl_stats)
            if meta: sources[entry["repo"]] = meta
            else: skipped.append(entry["repo"])
            rows.extend(mined); print(f"mined {entry['source']} {entry['repo']}: {len(mined)} raw candidates", file=sys.stderr)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError, OSError, zipfile.BadZipFile) as error:
            skipped.append(entry["repo"]); print(f"skip {entry['repo']}: {error}", file=sys.stderr)
        time.sleep(pause)
    _, survival = filter_rows(rows)
    if ddl: survival["sql_ddl"].update(ddl)
    write_jsonl(RAW / f"batch-{number:04d}.jsonl", rows)
    return {"batch": number, "repos": entries, "rows": len(rows), "class_counts": dict(collections.Counter(row["class"] for row in rows)), "per_type_counts": dict(collections.Counter(ptype(row) for row in rows if row["class"] == "positive")), "source_stats": {source: dict(values) for source, values in survival.items()}, "sources": sources, "skipped": skipped}

def goals_met(progress: dict[str, Any]) -> bool:
    types = progress.get("per_type_counts", {})
    return all(types.get(kind, 0) >= floor * 2 for kind, floor in RARE_TYPE_FLOORS.items()) and progress.get("repos_done", 0) >= 2_000

def collect(progress: dict[str, Any], pause: float) -> None:
    while progress["repos_remaining"] and not goals_met(progress):
        number = max(progress["completed_batches"] or [0]) + 1
        manifest = collect_batch(progress["repos_remaining"][:BATCH_SIZE], number, pause)
        atomic_json(RAW / f"batch-{number:04d}.manifest.json", manifest); apply_manifest(progress, manifest); atomic_json(PROGRESS, progress)
        print(json.dumps({"batch": number, "repos_done": progress["repos_done"], "repos_remaining": len(progress["repos_remaining"]), "per_type_counts": progress["per_type_counts"], "source_stats": progress["source_stats"]}, sort_keys=True), flush=True)

def load_raw() -> list[dict[str, Any]]:
    return [json.loads(line) for path in sorted(RAW.glob("batch-*.jsonl")) for line in path.read_text(encoding="utf-8").splitlines() if line]

def dedupe_cap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set(); by_sentence: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        key = json.dumps((row["source"]["repo"], row["source"]["path"], row["input"], row["target"]), sort_keys=True)
        if key not in seen: seen.add(key); by_sentence[row["input"]["sentence"]].append(row)
    output = []
    for sentence, values in by_sentence.items():
        for row in sorted(values, key=lambda row: (row["class"] != "positive", ptype(row), row_hash(row)))[:3]: output.append(row | {"frequency": len(values)})
    return output

def choose(rows: list[dict[str, Any]], limit: int, floors: dict[str, int]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows: grouped[row["class"]].append(row)
    max_total = min(limit, int(min(len(grouped["positive"])/.5, len(grouped["negative"])/.35, len(grouped["hard_negative"])/.15)))
    wanted = {"positive": round(max_total*.5), "negative": round(max_total*.35), "hard_negative": max_total - round(max_total*.5) - round(max_total*.35)}
    selected: list[dict[str, Any]] = []
    for kind, floor in floors.items():
        selected.extend(sorted((row for row in grouped["positive"] if ptype(row) == kind), key=row_hash)[:floor])
    chosen = {row_hash(row) for row in selected}
    for group, count in wanted.items():
        for row in sorted(grouped[group], key=lambda row: (repo_hash(row["source"]["repo"]), row_hash(row))):
            if len([x for x in selected if x["class"] == group]) >= count: break
            if row_hash(row) not in chosen: selected.append(row); chosen.add(row_hash(row))
    return sorted(selected, key=row_hash)

def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows: by_repo[row["source"]["repo"]].append(row)
    hold: set[str] = set(); held_rows = 0
    for repo in sorted(by_repo, key=repo_hash):
        if held_rows >= EVAL_TARGET_ROWS: break
        hold.add(repo); held_rows += len(by_repo[repo])
    return [row for row in rows if row["source"]["repo"] not in hold], [row for row in rows if row["source"]["repo"] in hold], sorted(hold)

def write_schema() -> None:
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Sidq multi-source claim record v3", "type": "object", "required": ["class", "frequency", "input", "source", "target"], "properties": {"class": {"enum": ["positive", "negative", "hard_negative"]}, "frequency": {"type": "integer", "minimum": 1}, "input": {"type": "object"}, "target": {"type": "object"}, "source": {"type": "object", "required": ["repo", "path", "commit", "licence", "collection_source"], "properties": {"repo": {"type": "string"}, "path": {"type": "string"}, "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"}, "licence": {"enum": sorted(ALLOWED_LICENSES)}, "collection_source": {"enum": list(SOURCE_NAMES)}}}}}
    atomic_json(OUT / "schema.json", schema)

def write_attribution(progress: dict[str, Any], released: list[dict[str, Any]]) -> None:
    released_repos = {row["source"]["repo"] for row in released}; lines = ["# Attribution", "", "Every admitted repository is pinned below. Each released pair carries its repo, path, commit, licence, and collection source.", "", "| Repository | Commit | Licence | Collection source | In train/eval |", "| --- | --- | --- | --- | --- |"]
    for repo, meta in sorted(progress["sources"].items()): lines.append(f"| {repo} | `{meta['commit']}` | {meta['licence']} | {', '.join(meta.get('collection_sources', []))} | {'yes' if repo in released_repos else 'no'} |")
    (OUT / "ATTRIBUTION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    notice = ["Sidq multi-source constraint-pair dataset", "", "All admitted repositories are identified in ATTRIBUTION.md. Root upstream NOTICE files from Apache-2.0 archives follow."]
    for repo, meta in sorted(progress["sources"].items()):
        if meta["licence"] == "Apache-2.0" and meta.get("notice"): notice.extend(["", f"--- {repo} @ {meta['commit']} ---", meta["notice"]])
    (OUT / "NOTICE").write_text("\n".join(notice) + "\n", encoding="utf-8")

def write_datasheet(progress: dict[str, Any], raw: list[dict[str, Any]], filtered: list[dict[str, Any]], released: list[dict[str, Any]], train: list[dict[str, Any]], evaluation: list[dict[str, Any]], held: list[str]) -> None:
    raw_by_source = collections.Counter(source_name(row) for row in raw); filt_by_source = collections.Counter(source_name(row) for row in filtered); types = collections.Counter(ptype(row) for row in released); distinct = len({row["input"]["sentence"] for row in released})
    lines = ["# Datasheet: multi-source constraint-pair dataset (v3)", "", "## Composition", "", f"The current release contains {len(released)} rows ({len(train)} train / {len(evaluation)} eval), with repository-level hold-out and deterministic seed `{SPLIT_SEED}`. Identical sentence text is capped at three rows. Distinct sentences: {distinct} / {len(released)} ({distinct / len(released):.2%} if released else 0).", "", "## Source survival", "", "| Source | Raw rows | Rows after claim-expression filter | Positive candidates | Positive survived | Positive survival |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for source in SOURCE_NAMES:
        stat = progress.get("source_stats", {}).get(source, {}); candidate, survived = stat.get("positive_candidates", 0), stat.get("positive_survived", 0); rate = f"{survived/candidate:.2%}" if candidate else "n/a"
        lines.append(f"| {source} | {raw_by_source[source]} | {filt_by_source[source]} | {candidate} | {survived} | {rate} |")
    ddl = progress.get("source_stats", {}).get("sql_ddl", {})
    lines.extend(["", f"SQL DDL constraints found without an attached human comment: {ddl.get('constraints_without_human_comment', 0)} of {ddl.get('constraints_found', 0)}. They are recorded as a collection finding and are never training pairs.", "", "## Per-type counts", "", "| Type | Released | Floor | Status |", "| --- | ---: | ---: | --- |"])
    for kind in PREDICATE_TYPES:
        floor = RARE_TYPE_FLOORS.get(kind, 0); status = "under-sampled" if floor and types[kind] < floor else "met / no floor"
        lines.append(f"| {kind} | {types[kind]} | {floor or '—'} | {status} |")
    lines.extend(["", "## Filtering and split", "", "Only a sentence that itself expresses the mapped constraint survives. Positive candidates whose label came only from an adjacent executable assertion are dropped. Negatives and hard-negatives are sampled at approximately 35% and 15%; the remaining approximately 50% is positive. No repository is shared between train and eval.", "", f"Held-out repositories ({len(held)}): " + ", ".join(held), "", "## Provenance and dropping", "", f"The crawl attempted {progress.get('repos_done', 0)} repositories; {len(progress.get('sources', {}))} passed the pinned permissive-licence gate. Dropped positive candidates are counted per source above; SQL constraints without prose are excluded; duplicate source records and sentences beyond the global three-occurrence cap are excluded. `ATTRIBUTION.md` and `NOTICE` contain provenance for every admitted repository.", ""])
    (OUT / "DATASHEET.md").write_text("\n".join(lines), encoding="utf-8")

def preserve_v2() -> None:
    for stem in ("train", "eval"):
        source, backup = OUT / f"{stem}.jsonl", OUT / f"{stem}-v2.jsonl"
        if source.exists() and not backup.exists(): shutil.copy2(source, backup)

def finalize(progress: dict[str, Any]) -> int:
    raw = load_raw()
    if not raw: raise ValueError("no raw-v3 batches yet")
    filtered, _ = filter_rows(raw); capped = dedupe_cap(filtered); train_pool, eval_pool, held = split(capped)
    evaluation = choose(eval_pool, EVAL_TARGET_ROWS, {kind: min(EVAL_MIN_PER_TYPE, RARE_TYPE_FLOORS.get(kind, 0)) for kind in PREDICATE_TYPES})
    train = choose(train_pool, TARGET_ROWS - len(evaluation), RARE_TYPE_FLOORS); released = train + evaluation
    preserve_v2(); write_jsonl(OUT / "train.jsonl", train); write_jsonl(OUT / "eval.jsonl", evaluation); write_schema(); write_attribution(progress, released); write_datasheet(progress, raw, filtered, released, train, evaluation, held)
    counts = collections.Counter(ptype(row) for row in released); print(json.dumps({"total": len(released), "types": counts, "under_sampled": {kind: max(0, floor-counts[kind]) for kind, floor in RARE_TYPE_FLOORS.items()}}, default=dict, sort_keys=True))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(description="resumable licence-audited multi-source claim miner")
    parser.add_argument("--resume", action="store_true"); parser.add_argument("--finalize", action="store_true")
    parser.add_argument("--repo-count", type=int, default=2600); parser.add_argument("--pause", type=float, default=.15)
    parser.add_argument("--plan-only", action="store_true"); parser.add_argument("--expand-sourcegraph", action="store_true", help="append source-tagged public-code candidates on a later resume")
    args = parser.parse_args(); OUT.mkdir(parents=True, exist_ok=True); RAW.mkdir(parents=True, exist_ok=True)
    if args.repo_count < 20: parser.error("--repo-count must be at least 20")
    if not PROGRESS.exists():
        progress = initial_progress(discover(args.repo_count)); atomic_json(PROGRESS, progress)
    else: progress = reconcile(json.loads(PROGRESS.read_text(encoding="utf-8")))
    if args.expand_sourcegraph:
        added = expand_plan(progress, args.repo_count)
        atomic_json(PROGRESS, progress)
        print(json.dumps({"sourcegraph_candidates_added": added, "planned": len(progress["planned_repos"])}, sort_keys=True))
    if args.finalize: return finalize(progress)
    if args.plan_only:
        print(json.dumps({"planned": len(progress["planned_repos"]), "done": progress["repos_done"], "remaining": len(progress["repos_remaining"])}, sort_keys=True)); return 0
    if not args.resume and progress["repos_done"]: parser.error("existing v3 collection: use --resume")
    lock()
    try: collect(progress, args.pause)
    finally: unlock()
    return 0

if __name__ == "__main__": raise SystemExit(main())
