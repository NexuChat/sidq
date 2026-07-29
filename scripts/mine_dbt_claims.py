#!/usr/bin/env python3
"""Checkpointed, licence-audited miner for dbt documentation claim pairs.

Collection is deliberately separate from finalisation.  Every completed group
of twenty repositories is written to ``data/claims/raw/batch-NNN.jsonl`` and
its manifest is committed to ``raw/_progress.json``.  ``--resume`` therefore
continues at the next unfinished repository rather than repeating a crawl.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import os
import re
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
RAW = OUT / "raw"
PROGRESS = RAW / "_progress.json"
COLLECTION_LOCK = RAW / ".mine.lock"
GITHUB_CANDIDATES = RAW / "_github_candidates.json"
SCHEMA_VERSION = "1.0.0"
SPLIT_SEED = "sidq-dbt-claims-v1"
BATCH_SIZE = 20
MIN_SOURCE_REPOS = 300
EVAL_TARGET_ROWS = 400
EVAL_MIN_PER_TYPE = 40
EVAL_MIN_NO_CLAIM = 120
GITHUB_API_EXHAUSTED = False
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Unlicense", "CC0-1.0"}
PREDICATE_TYPES = ("not_null", "unique", "accepted_values", "relationships", "expression")
EVAL_TYPE_FLOORS = {kind: EVAL_MIN_PER_TYPE for kind in PREDICATE_TYPES}
RARE_TYPE_FLOORS = {"accepted_values": 150, "relationships": 100, "expression": 100}
TYPE_PRIORITY = {kind: index for index, kind in enumerate(("accepted_values", "relationships", "expression", "unique", "not_null"))}
HARD_NEGATIVE_RE = re.compile(r"\b(source of truth|updated (regularly|daily|weekly|monthly|hourly)|important (for|to)|used (for|by)|powers?|supports?|enables?|contains? (information|data)|tracks?|represents?|stores?|provides?|central|canonical|primary|key business)\b", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`])")

# These high-yield dbt packages are followed by repository discovery.  Every
# candidate is still checked against the exact pinned archive's top-level
# licence; the list is not a licence allowlist.
REPOSITORIES = (
    "dbt-labs/jaffle_shop", "dbt-labs/dbt-utils", "dbt-labs/dbt-audit-helper", "dbt-labs/dbt-codegen",
    "dbt-labs/dbt-project-evaluator", "dbt-labs/dbt-external-tables", "dbt-checkpoint/dbt-checkpoint",
    "calogica/dbt-date", "calogica/dbt-expectations", "calogica/dbt-snowflake-monitoring",
    "Metaplane/dbt-expectations", "Datavault-UK/automate-dv", "Data-Coves/dbt-coves",
    "fivetran/dbt_fivetran_utils", "fivetran/dbt_fivetran_log", "fivetran/dbt_shopify", "fivetran/dbt_stripe",
    "fivetran/dbt_google_ads", "fivetran/dbt_facebook_ads", "fivetran/dbt_hubspot", "fivetran/dbt_zendesk",
    "fivetran/dbt_salesforce", "fivetran/dbt_linkedin_ads", "fivetran/dbt_twitter_ads", "fivetran/dbt_marketo",
    "fivetran/dbt_pinterest", "fivetran/dbt_tiktok_ads", "fivetran/dbt_amplitude", "fivetran/dbt_apple_store",
    "fivetran/dbt_klaviyo", "fivetran/dbt_intercom", "fivetran/dbt_github", "fivetran/dbt_netsuite",
    "fivetran/dbt_greenhouse", "fivetran/dbt_drift", "fivetran/dbt_quickbooks", "fivetran/dbt_qualtrics",
    "fivetran/dbt_lever", "fivetran/dbt_workday", "fivetran/dbt_recharge", "fivetran/dbt_mailchimp",
    "fivetran/dbt_pardot", "fivetran/dbt_twilio", "fivetran/dbt_asana", "fivetran/dbt_sap",
    "fivetran/dbt_recurly", "fivetran/dbt_zuora", "fivetran/dbt_microsoft_ads", "snowplow/dbt-snowplow-web",
    "snowplow/dbt-snowplow-mobile", "snowplow/dbt-snowplow-media-player", "data-mie/dbt_artifacts",
)
DISCOVERY_QUERIES = ("dbt in:name", "topic:dbt", "dbt in:description", "\"dbt package\" in:description", "\"data build tool\" in:description")
PACKAGE_HUB_INDEX = "https://hub.getdbt.com/api/v1/index.json"
SOURCEGRAPH_SEARCH = "https://sourcegraph.com/.api/search/stream"
TARGETED_SEARCH_QUERIES = (
    "accepted_values lang:yaml count:100",
    "expression_is_true lang:yaml count:100",
    "accepted_range lang:yaml count:100",
    "expect_column_values_to_ lang:yaml count:100",
    "relationships lang:yaml count:100",
)


@dataclass(frozen=True)
class RepoInfo:
    name: str
    commit: str
    licence: str
    default_branch: str


@dataclass
class MinedRepository:
    records: list[dict[str, Any]]
    notice: str | None
    info: RepoInfo


def request_json(url: str) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "sidq-dbt-claim-miner/2.0"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_repositories(limit: int) -> list[str]:
    found, seen = list(REPOSITORIES), set(REPOSITORIES)
    for query in DISCOVERY_QUERIES:
        for page in range(1, 11):
            if len(found) >= limit:
                return found[:limit]
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
            try:
                items = request_json(url).get("items", [])
            except urllib.error.HTTPError as error:
                print(f"discovery stopped for {query!r}: {error}", file=sys.stderr)
                break
            if not isinstance(items, list) or not items:
                break
            for item in items:
                name = item.get("full_name") if isinstance(item, dict) else None
                if isinstance(name, str) and name.count("/") == 1 and name not in seen:
                    found.append(name); seen.add(name)
                    if len(found) >= limit:
                        return found[:limit]
    return found[:limit]


def package_hub_repositories() -> list[str]:
    """Return the public dbt package registry's repository coordinates.

    The registry is used only to form a persisted crawl plan.  Each repository
    still has to pass the pinned-archive licence gate before any source text is
    retained.
    """
    try:
        payload = request_json(PACKAGE_HUB_INDEX)
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        print(f"package registry expansion unavailable: {error}", file=sys.stderr)
        return []
    return [repo for repo in payload if isinstance(repo, str) and repo.count("/") == 1]


def targeted_search_repositories() -> list[str]:
    """Find public repositories mentioning the rare executable test families.

    GitHub's unauthenticated code search is not available to this collector.
    This search only discovers candidates; pinned GitHub archives remain the
    source of record and must pass the exact same licence audit as every other
    repository.
    """
    found: list[str] = []
    seen: set[str] = set()
    for query in TARGETED_SEARCH_QUERIES:
        url = SOURCEGRAPH_SEARCH + "?" + urllib.parse.urlencode({"q": f"context:global {query}", "v": "V3"})
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "sidq-dbt-claim-miner/2.0"})
            with urllib.request.urlopen(request, timeout=60) as response:
                stream = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            print(f"targeted search unavailable for {query!r}: {error}", file=sys.stderr)
            continue
        for repo in re.findall(r'"repository":"github\.com/([^"]+)"', stream):
            if repo.count("/") == 1 and repo not in seen:
                found.append(repo)
                seen.add(repo)
    return found


def github_candidate_repositories() -> list[str]:
    """Load authenticated GitHub code-search results persisted by the crawl operator."""
    if not GITHUB_CANDIDATES.exists():
        print(f"GitHub candidate expansion unavailable: {GITHUB_CANDIDATES} is missing", file=sys.stderr)
        return []
    try:
        payload = json.loads(GITHUB_CANDIDATES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"GitHub candidate expansion unavailable: {error}", file=sys.stderr)
        return []
    candidates = payload.get("candidates", [])
    return [
        repo
        for item in candidates
        if isinstance(item, (str, dict))
        and isinstance(repo := (item if isinstance(item, str) else item.get("repo")), str)
        and repo.count("/") == 1
    ]


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


def archive_fallback_info(repo: str) -> RepoInfo | None:
    for branch in ("main", "master", "develop", "trunk"):
        try:
            request = urllib.request.Request(f"https://codeload.github.com/{repo}/zip/{branch}", headers={"User-Agent": "sidq-dbt-claim-miner/2.0"})
            with urllib.request.urlopen(request, timeout=120) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
        except urllib.error.HTTPError as error:
            if error.code == 404: continue
            raise
        commit = archive.comment.decode("ascii", errors="ignore")
        licence = archive_licence(archive)
        if not re.fullmatch(r"[0-9a-f]{40}", commit): raise KeyError("archive did not provide an exact commit SHA")
        if licence not in ALLOWED_LICENSES:
            print(f"skip {repo}: licence={licence or 'missing/ambiguous'}", file=sys.stderr); return None
        return RepoInfo(repo, commit, licence, branch)
    print(f"skip {repo}: no main/master/develop/trunk archive", file=sys.stderr)
    return None


def fetch_repo_info(repo: str) -> RepoInfo | None:
    global GITHUB_API_EXHAUSTED
    if GITHUB_API_EXHAUSTED:
        return archive_fallback_info(repo)
    try:
        metadata = request_json(f"https://api.github.com/repos/{repo}")
        licence = (metadata.get("license") or {}).get("spdx_id")
        if licence not in ALLOWED_LICENSES:
            print(f"skip {repo}: licence={licence or 'missing/ambiguous'}", file=sys.stderr); return None
        branch = metadata["default_branch"]
        commit = request_json(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch)}")["sha"]
        return RepoInfo(repo, commit, licence, branch)
    except urllib.error.HTTPError as error:
        if error.code != 403: raise
        GITHUB_API_EXHAUSTED = True
        return archive_fallback_info(repo)


def fetch_archive(info: RepoInfo) -> zipfile.ZipFile:
    request = urllib.request.Request(f"https://codeload.github.com/{info.name}/zip/{info.commit}", headers={"User-Agent": "sidq-dbt-claim-miner/2.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return zipfile.ZipFile(io.BytesIO(response.read()))


def schema_paths(archive: zipfile.ZipFile) -> Iterable[str]:
    for name in archive.namelist():
        lower, basename = name.lower(), name.lower().rsplit("/", 1)[-1]
        if basename.endswith((".yml", ".yaml")) and basename not in {"packages.yml", "dependencies.yml", "dbt_project.yml"} and "/target/" not in lower and "/dbt_packages/" not in lower:
            yield name


def text_value(value: Any) -> str | None:
    if not isinstance(value, str): return None
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) >= 8 and not value.startswith("{{") else None


def sentences(description: str) -> list[str]:
    values = [part.strip(" -\t\n") for part in SENTENCE_RE.split(description) if len(part.strip()) >= 8]
    return values or [description]


def test_items(value: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    if not isinstance(value, list): return
    for item in value:
        if isinstance(item, str): yield item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, config = next(iter(item.items()))
            if isinstance(name, str): yield name, config if isinstance(config, dict) else {}


def node_tests(node: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    yield from test_items(node.get("tests"))
    yield from test_items(node.get("data_tests"))


def clean_values(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) and value and all(isinstance(item, (str, int, float, bool)) for item in value) else None


def literal(value: Any) -> str | None:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (str, int, float, bool)) and not (isinstance(value, str) and "{{" in value) else None


def test_arguments(config: dict[str, Any]) -> dict[str, Any]:
    arguments = config.get("arguments")
    if not isinstance(arguments, dict): return config
    return config | arguments


def range_expression(args: dict[str, Any]) -> str | None:
    bounds = []
    for key, label in (("min_value", "min"), ("max_value", "max"), ("value", "value")):
        if key in args:
            if (item := literal(args[key])) is None: return None
            bounds.append(f"{label}={item}")
    return f"range({';'.join(bounds)})" if bounds else None


def map_test(name: str, config: dict[str, Any], column: str | None) -> tuple[dict[str, Any] | None, str | None]:
    short, args = name.rsplit(".", 1)[-1], test_arguments(config)
    if short in {"unique", "not_null"}:
        return ({"type": short, "column": column}, None) if column else (None, "model_level_unique_or_not_null")
    if short == "accepted_values":
        values = clean_values(args.get("values"))
        return ({"type": "accepted_values", "column": column, "values": values}, None) if column and values is not None else (None, "accepted_values_missing_literal_values")
    if short == "relationships":
        to, field = args.get("to"), args.get("field")
        return ({"type": "relationships", "column": column, "expr": f"to={to};field={field}"}, None) if column and isinstance(to, str) and isinstance(field, str) else (None, "relationships_missing_to_or_field")
    if short == "expression_is_true":
        expression = args.get("expression")
        return ({"type": "expression", "column": column, "expr": expression.strip()}, None) if column and isinstance(expression, str) and expression.strip() else (None, "expression_missing_literal")
    if short == "accepted_range":
        expression = range_expression(args)
        return ({"type": "expression", "column": column, "expr": expression}, None) if column and expression else (None, "accepted_range_missing_literal_bounds")
    if short.startswith("expect_column_values_to_"):
        if short == "expect_column_values_to_be_in_set":
            values = clean_values(args.get("value_set", args.get("values")))
            return ({"type": "accepted_values", "column": column, "values": values}, None) if column and values is not None else (None, "expectation_set_missing_literal_values")
        suffixes = ("be_between", "be_greater_than", "be_greater_than_or_equal_to", "be_less_than", "be_less_than_or_equal_to")
        if any(short.endswith(suffix) for suffix in suffixes):
            expression = range_expression(args)
            return ({"type": "expression", "column": column, "expr": expression}, None) if column and expression else (None, "range_expectation_missing_literal_bounds")
        return None, f"unmapped_expectation:{name}"
    return None, f"unmapped_test:{name}"


def context_for(model: dict[str, Any], column: dict[str, Any] | None) -> str:
    parts = []
    if description := text_value(model.get("description")): parts.append(f"table_description={description[:500]}")
    if column and isinstance(column.get("data_type"), str): parts.append(f"data_type={column['data_type']}")
    return "; ".join(parts) or "dbt schema.yml"


def base_record(sentence: str, table: str, column: str | None, context: str, info: RepoInfo, path: str) -> dict[str, Any]:
    return {"input": {"sentence": sentence, "column_name": column, "table_name": table, "schema_context": context}, "source": {"repo": info.name, "path": path, "commit": info.commit, "licence": info.licence}}


def mine_schema(doc: Any, info: RepoInfo, path: str, dropped: collections.Counter[str]) -> list[dict[str, Any]]:
    if not isinstance(doc, dict): return []
    records: list[dict[str, Any]] = []
    for section in ("models", "sources", "seeds", "snapshots"):
        nodes = doc.get(section)
        if not isinstance(nodes, list): continue
        resources = []
        for node in nodes:
            if not isinstance(node, dict): continue
            if section == "sources" and isinstance(node.get("tables"), list):
                for table in node["tables"]:
                    if isinstance(table, dict): resources.append(table | {"description": table.get("description", node.get("description"))})
            else: resources.append(node)
        for model in resources:
            if not isinstance(model.get("name"), str): continue
            table, model_description = model["name"], text_value(model.get("description"))
            mapped_model = []
            model_declared = list(node_tests(model))
            for name, config in model_declared:
                predicate, reason = map_test(name, config, None)
                if predicate: mapped_model.append(predicate)
                else: dropped[reason or "unknown"] += 1
            if model_description and not model_declared:
                for sentence in sentences(model_description):
                    base = base_record(sentence, table, None, context_for(model, None), info, path)
                    records.append(base | {"target": {"claim": None}, "class": "hard_negative" if HARD_NEGATIVE_RE.search(sentence) else "negative"})
            elif model_description and mapped_model:
                for sentence in sentences(model_description):
                    base = base_record(sentence, table, None, context_for(model, None), info, path)
                    records.extend(base | {"target": {"claim": predicate}, "class": "positive"} for predicate in mapped_model)
            for column in model.get("columns", []) if isinstance(model.get("columns"), list) else []:
                if not isinstance(column, dict) or not isinstance(column.get("name"), str) or not (description := text_value(column.get("description"))): continue
                name, declared, mapped = column["name"], list(node_tests(column)), []
                for test_name, config in declared:
                    predicate, reason = map_test(test_name, config, name)
                    if predicate: mapped.append(predicate)
                    else: dropped[reason or "unknown"] += 1
                if declared and not mapped: continue
                for sentence in sentences(description):
                    base = base_record(sentence, table, name, context_for(model, column), info, path)
                    if mapped: records.extend(base | {"target": {"claim": predicate}, "class": "positive"} for predicate in mapped)
                    else: records.append(base | {"target": {"claim": None}, "class": "hard_negative" if HARD_NEGATIVE_RE.search(sentence) else "negative"})
    return records


def archive_notice(archive: zipfile.ZipFile) -> str | None:
    for member in archive.infolist():
        bits = member.filename.split("/", 1)
        if len(bits) == 2 and bits[1].upper() == "NOTICE": return archive.read(member).decode("utf-8", errors="replace").strip() or None
    return None


def mine_repo(info: RepoInfo, dropped: collections.Counter[str]) -> MinedRepository:
    archive, records = fetch_archive(info), []
    # GitHub metadata is useful for discovery, but the archive is the actual
    # pinned source being redistributed.  Gate and label with its licence.
    licence = archive_licence(archive)
    if licence not in ALLOWED_LICENSES:
        raise ValueError(f"pinned archive licence={licence or 'missing/ambiguous'}")
    info = RepoInfo(info.name, info.commit, licence, info.default_branch)
    root = archive.namelist()[0].split("/", 1)[0] + "/"
    for path in schema_paths(archive):
        try: document = yaml.safe_load(archive.read(path))
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as error:
            print(f"skip unreadable {info.name}:{path}: {error}", file=sys.stderr); continue
        records.extend(mine_schema(document, info, path.removeprefix(root), dropped))
    return MinedRepository(records, archive_notice(archive), info)


def predicate_type(record: dict[str, Any]) -> str | None:
    claim = record.get("target", {}).get("claim")
    return claim.get("type") if isinstance(claim, dict) else None


def record_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{SPLIT_SEED}:{payload}".encode()).hexdigest()


def repository_hash(repo: str) -> str:
    return hashlib.sha256(f"{SPLIT_SEED}:repository:{repo}".encode()).hexdigest()


def record_key(record: dict[str, Any]) -> tuple[str, str, str, str | None, str, str]:
    return (record["source"]["repo"], record["source"]["path"], record["input"]["table_name"], record["input"].get("column_name"), record["input"]["sentence"], json.dumps(record["target"]["claim"], sort_keys=True))


def deduplicate(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    winners: dict[tuple[str, str, str, str | None, str, str], dict[str, Any]] = {}
    for record in records:
        key = record_key(record)
        if key not in winners or record_hash(record) < record_hash(winners[key]): winners[key] = record
    return list(winners.values())


def type_counts(records: Iterable[dict[str, Any]]) -> collections.Counter[str]:
    return collections.Counter(predicate_type(record) or "no-claim" for record in records)


def diversity_cap(records: list[dict[str, Any]], cap: int = 3) -> tuple[list[dict[str, Any]], dict[str, int]]:
    frequencies = collections.Counter(record["input"]["sentence"] for record in records)
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    exact = deduplicate(records)
    for record in exact: grouped[record["input"]["sentence"]].append(record)
    capped = []
    for sentence, values in grouped.items():
        ranked = sorted(values, key=lambda row: (row["class"] != "positive", TYPE_PRIORITY.get(predicate_type(row) or "", 99), record_hash(row)))
        capped.extend(row | {"frequency": frequencies[sentence]} for row in ranked[:cap])
    return capped, {"raw_rows": len(records), "raw_distinct_sentences": len(frequencies), "raw_distinct_sentence_target_pairs": len({(row["input"]["sentence"], json.dumps(row["target"]["claim"], sort_keys=True)) for row in records}), "exact_rows": len(exact), "capped_rows": len(capped), "capped_distinct_sentences": len({row["input"]["sentence"] for row in capped}), "capped_distinct_sentence_target_pairs": len({(row["input"]["sentence"], json.dumps(row["target"]["claim"], sort_keys=True)) for row in capped})}


def sample_across_repositories(rows: Iterable[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Deterministically round-robin repositories so large packages cannot dominate."""
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[row["source"]["repo"]].append(row)
    for values in groups.values():
        values.sort(key=record_hash)
    repos = sorted(groups, key=repository_hash)
    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < count:
        added = False
        for repo in repos:
            if depth < len(groups[repo]):
                selected.append(groups[repo][depth])
                added = True
                if len(selected) == count:
                    break
        if not added:
            break
        depth += 1
    return selected


def select_positive(rows: list[dict[str, Any]], count: int, floors: dict[str, int]) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        if predicate_type(row) in PREDICATE_TYPES: by_type[predicate_type(row)].append(row)
    selected, keys = [], set()
    for kind in PREDICATE_TYPES:
        for row in sample_across_repositories(by_type[kind], floors.get(kind, 0)):
            if record_key(row) not in keys: selected.append(row); keys.add(record_key(row))
    if len(selected) > count: return selected
    selected.extend(sample_across_repositories((row for row in rows if record_key(row) not in keys), count - len(selected)))
    return selected


def choose_records(rows: list[dict[str, Any]], limit: int, floors: dict[str, int] | None = None) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in deduplicate(rows): groups[row["class"]].append(row)
    shares = {"positive": .50, "negative": .35, "hard_negative": .15}
    total = min(limit, int(min(len(groups[kind]) / share for kind, share in shares.items())))
    wanted = {"positive": total * 50 // 100, "negative": total * 35 // 100, "hard_negative": total * 15 // 100}; wanted["positive"] += total - sum(wanted.values())
    selected = select_positive(groups["positive"], wanted["positive"], floors or {})
    for kind in ("negative", "hard_negative"):
        selected.extend(sample_across_repositories(groups[kind], wanted[kind]))
    return sorted(selected, key=record_hash)


def split_by_repository(rows: list[dict[str, Any]], target_eval: int = EVAL_TARGET_ROWS) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Reserve whole repositories until the eval floors and class mix are feasible."""
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows: by_repo[row["source"]["repo"]].append(row)
    counts = {repo: collections.Counter(row["class"] for row in value) for repo, value in by_repo.items()}
    types = {repo: type_counts(value) for repo, value in by_repo.items()}
    quotas = {"positive": target_eval * 50 // 100, "negative": target_eval * 35 // 100, "hard_negative": target_eval * 15 // 100}
    held_out: list[str] = []
    selected_types: collections.Counter[str] = collections.Counter()
    selected_classes: collections.Counter[str] = collections.Counter()

    def type_gain(repo: str) -> int:
        return sum(min(types[repo][kind], max(0, EVAL_TYPE_FLOORS[kind] - selected_types[kind])) for kind in PREDICATE_TYPES)

    def type_excess(repo: str) -> int:
        return sum(max(0, types[repo][kind] - max(0, EVAL_TYPE_FLOORS[kind] - selected_types[kind])) for kind in PREDICATE_TYPES)

    def class_gain(repo: str) -> int:
        return sum(min(counts[repo][kind], max(0, quotas[kind] - selected_classes[kind])) for kind in quotas)

    while any(selected_types[kind] < floor for kind, floor in EVAL_TYPE_FLOORS.items()):
        candidates = [repo for repo in by_repo if repo not in held_out and type_gain(repo)]
        if not candidates: break
        # Prefer a repository that meets a remaining floor without withholding
        # a disproportionate share of a rare type from training.
        winner = max(candidates, key=lambda repo: (10 * type_gain(repo) - type_excess(repo), type_gain(repo), class_gain(repo), -sum(counts[repo].values()), repository_hash(repo)))
        held_out.append(winner); selected_types += types[winner]; selected_classes += counts[winner]
    while any(selected_classes[kind] < quota for kind, quota in quotas.items()):
        candidates = [repo for repo in by_repo if repo not in held_out]
        if not candidates: break
        winner = max(candidates, key=lambda repo: (class_gain(repo), -sum(counts[repo].values()), repository_hash(repo)))
        if class_gain(winner) == 0: break
        held_out.append(winner); selected_types += types[winner]; selected_classes += counts[winner]
    hold = set(held_out)
    return [row for row in rows if row["source"]["repo"] not in hold], [row for row in rows if row["source"]["repo"] in hold], sorted(held_out)


def build_splits(raw: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], dict[str, int]]:
    capped, diversity = diversity_cap(raw)
    train_candidates, eval_candidates, held_out = split_by_repository(capped)
    evaluation = choose_records(eval_candidates, min(EVAL_TARGET_ROWS, limit), EVAL_TYPE_FLOORS)
    train = choose_records(train_candidates, max(0, limit - len(evaluation)), RARE_TYPE_FLOORS)
    return train, evaluation, held_out, diversity


def add_released_diversity(diversity: dict[str, int], rows: list[dict[str, Any]]) -> dict[str, int]:
    diversity = dict(diversity)
    diversity.update(
        {
            "released_rows": len(rows),
            "released_distinct_sentences": len({row["input"]["sentence"] for row in rows}),
            "released_distinct_sentence_target_pairs": len(
                {
                    (
                        row["input"]["sentence"],
                        json.dumps(row["target"]["claim"], sort_keys=True),
                    )
                    for row in rows
                }
            ),
        }
    )
    return diversity


def missing_types(train: Iterable[dict[str, Any]], evaluation: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    return {"train": [kind for kind in PREDICATE_TYPES if not any(predicate_type(row) == kind for row in train)], "eval": [kind for kind in PREDICATE_TYPES if not any(predicate_type(row) == kind for row in evaluation)]}


def evaluation_shortfalls(evaluation: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = type_counts(evaluation)
    required = EVAL_TYPE_FLOORS | {"no-claim": EVAL_MIN_NO_CLAIM}
    return {kind: max(0, minimum - counts[kind]) for kind, minimum in required.items()}


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows: handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def initial_progress(repositories: list[str]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "batch_size": BATCH_SIZE, "planned_repos": repositories, "completed_repos": [], "repos_done": 0, "repos_remaining": repositories.copy(), "completed_batches": [], "class_counts": {}, "per_type_counts": {kind: 0 for kind in PREDICATE_TYPES}, "dropped": {}, "skipped": [], "sources": {}}


def expand_plan(progress: dict[str, Any], repositories: Iterable[str], limit: int) -> bool:
    """Append unseen candidates without changing progress already on disk."""
    planned = list(progress["planned_repos"])
    seen = set(planned)
    for repo in repositories:
        if len(planned) >= limit:
            break
        if repo not in seen:
            planned.append(repo)
            seen.add(repo)
    if planned == progress["planned_repos"]:
        return False
    done = set(progress.get("completed_repos", []))
    progress["planned_repos"] = planned
    progress["repos_done"] = len(done)
    progress["repos_remaining"] = [repo for repo in planned if repo not in done]
    return True


def read_progress() -> dict[str, Any]:
    if not PROGRESS.exists(): raise FileNotFoundError(f"no progress file at {PROGRESS}; start a collection first")
    return json.loads(PROGRESS.read_text(encoding="utf-8"))


def apply_manifest(progress: dict[str, Any], manifest: dict[str, Any]) -> None:
    if manifest["batch"] in progress.get("completed_batches", []): return
    progress.setdefault("completed_batches", []).append(manifest["batch"])
    progress.setdefault("completed_repos", []).extend(manifest["repos"])
    progress["completed_repos"] = list(dict.fromkeys(progress["completed_repos"]))
    done = set(progress["completed_repos"]); progress["repos_done"] = len(done); progress["repos_remaining"] = [repo for repo in progress["planned_repos"] if repo not in done]
    for field in ("class_counts", "per_type_counts", "dropped"):
        counter = collections.Counter(progress.get(field, {})); counter.update(manifest.get(field, {})); progress[field] = dict(counter)
    progress["per_type_counts"] = {kind: progress["per_type_counts"].get(kind, 0) for kind in PREDICATE_TYPES}
    progress["skipped"] = sorted(set(progress.get("skipped", [])) | set(manifest.get("skipped", [])))
    progress.setdefault("sources", {}).update(manifest.get("sources", {}))


def reconcile_progress(progress: dict[str, Any]) -> dict[str, Any]:
    changed = False
    for path in sorted(RAW.glob("batch-*.manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["batch"] not in progress.get("completed_batches", []): apply_manifest(progress, manifest); changed = True
    if changed: atomic_json(PROGRESS, progress)
    return progress


def reset_collection() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for path in list(RAW.glob("batch-*.jsonl")) + list(RAW.glob("batch-*.manifest.json")): path.unlink()
    PROGRESS.unlink(missing_ok=True)
    (OUT / ".candidates.jsonl").unlink(missing_ok=True); (OUT / ".collection_stats.json").unlink(missing_ok=True)


def acquire_collection_lock() -> None:
    """Prevent two resumptions from choosing the same next batch number."""
    RAW.mkdir(parents=True, exist_ok=True)
    if COLLECTION_LOCK.exists():
        try:
            pid = int(json.loads(COLLECTION_LOCK.read_text(encoding="utf-8")).get("pid", -1))
            os.kill(pid, 0)
        except (ValueError, json.JSONDecodeError, ProcessLookupError):
            COLLECTION_LOCK.unlink(missing_ok=True)
        except PermissionError:
            raise RuntimeError(f"collection already running (pid {pid})")
        else:
            raise RuntimeError(f"collection already running (pid {pid})")
    try:
        descriptor = os.open(COLLECTION_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise RuntimeError("collection already running") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "started_at": int(time.time())}, handle)


def release_collection_lock() -> None:
    try:
        lock = json.loads(COLLECTION_LOCK.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if lock.get("pid") == os.getpid():
        COLLECTION_LOCK.unlink(missing_ok=True)


def collect_batch(repositories: list[str], number: int, pause: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []; dropped: collections.Counter[str] = collections.Counter(); skipped: list[str] = []; sources: dict[str, dict[str, Any]] = {}
    for repo in repositories:
        try:
            info = fetch_repo_info(repo)
            if info is None: skipped.append(repo)
            else:
                mined = mine_repo(info, dropped); rows.extend(mined.records)
                sources[repo] = {"commit": mined.info.commit, "licence": mined.info.licence, "notice": mined.notice}
                print(f"mined {repo}: {len(mined.records)} candidate rows", file=sys.stderr)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, TimeoutError, OSError, zipfile.BadZipFile) as error:
            print(f"skip {repo}: {error}", file=sys.stderr); skipped.append(repo)
        time.sleep(pause)
    write_jsonl(RAW / f"batch-{number:03d}.jsonl", rows)
    return {"batch": number, "repos": repositories, "rows": len(rows), "class_counts": dict(collections.Counter(row["class"] for row in rows)), "per_type_counts": dict(type_counts(rows)), "dropped": dict(dropped), "skipped": skipped, "sources": sources}


def collection_goals_met(progress: dict[str, Any]) -> bool:
    per_type = progress.get("per_type_counts", {})
    return (
        len(progress.get("sources", {})) >= MIN_SOURCE_REPOS
        and all(per_type.get(kind, 0) >= floor for kind, floor in RARE_TYPE_FLOORS.items())
    )


def collect(progress: dict[str, Any], pause: float) -> None:
    while progress["repos_remaining"] and not collection_goals_met(progress):
        number = max(progress.get("completed_batches") or [0]) + 1
        manifest = collect_batch(progress["repos_remaining"][:BATCH_SIZE], number, pause)
        atomic_json(RAW / f"batch-{number:03d}.manifest.json", manifest)
        apply_manifest(progress, manifest); atomic_json(PROGRESS, progress)
        print(json.dumps({"batch": number, "repos_done": progress["repos_done"], "repos_remaining": len(progress["repos_remaining"]), "per_type_counts": progress["per_type_counts"]}, sort_keys=True))
    if collection_goals_met(progress):
        print(json.dumps({"collection_goals_met": True, "admitted_sources": len(progress.get("sources", {})), "per_type_counts": progress["per_type_counts"]}, sort_keys=True))


def write_schema() -> None:
    column_claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "column"],
        "properties": {
            "type": {"enum": ["not_null", "unique"]},
            "column": {"type": "string", "minLength": 1},
        },
    }
    values_claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "column", "values"],
        "properties": {
            "type": {"const": "accepted_values"},
            "column": {"type": "string", "minLength": 1},
            "values": {
                "type": "array",
                "minItems": 1,
                "items": {"type": ["string", "number", "boolean"]},
            },
        },
    }
    expression_claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "column", "expr"],
        "properties": {
            "type": {"enum": ["relationships", "expression"]},
            "column": {"type": "string", "minLength": 1},
            "expr": {"type": "string", "minLength": 1},
        },
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://sidq.dev/schemas/dbt-documentation-claim-record-1.0.0.json",
        "title": "Sidq dbt documentation claim record",
        "type": "object",
        "additionalProperties": False,
        "required": ["class", "frequency", "input", "source", "target"],
        "properties": {
            "class": {"enum": ["positive", "negative", "hard_negative"]},
            "frequency": {
                "type": "integer",
                "minimum": 1,
                "description": "Occurrences of the exact sentence before the global cap of three.",
            },
            "input": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sentence", "column_name", "table_name", "schema_context"],
                "properties": {
                    "sentence": {"type": "string", "minLength": 8},
                    "column_name": {"type": ["string", "null"]},
                    "table_name": {"type": "string", "minLength": 1},
                    "schema_context": {"type": "string"},
                },
            },
            "source": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repo", "path", "commit", "licence"],
                "properties": {
                    "repo": {"type": "string", "pattern": "^[^/]+/[^/]+$"},
                    "path": {"type": "string", "minLength": 1},
                    "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
                    "licence": {"enum": sorted(ALLOWED_LICENSES)},
                },
            },
            "target": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim"],
                "properties": {
                    "claim": {
                        "oneOf": [
                            {"type": "null"},
                            column_claim,
                            values_claim,
                            expression_claim,
                        ]
                    }
                },
            },
        },
        "allOf": [
            {
                "if": {"properties": {"class": {"const": "positive"}}},
                "then": {
                    "properties": {
                        "target": {
                            "properties": {"claim": {"not": {"type": "null"}}}
                        }
                    }
                },
                "else": {
                    "properties": {
                        "target": {
                            "properties": {"claim": {"type": "null"}}
                        }
                    }
                },
            }
        ],
    }
    atomic_json(OUT / "schema.json", schema)


def write_attribution(rows: list[dict[str, Any]], sources: dict[str, dict[str, Any]]) -> None:
    released_repos = {row["source"]["repo"] for row in rows}
    admitted = sorted(
        (repo, source["commit"], source["licence"])
        for repo, source in sources.items()
    )
    attribution = [
        "# Attribution",
        "",
        "The raw checkpoints and derived train/eval files were assembled from the pinned, licence-admitted repositories below. A repository can have no released pair after de-duplication, the sentence-frequency cap, class balancing, and repository hold-out.",
        "",
        "| Repository | Commit | Licence | In train/eval |",
        "| --- | --- | --- | --- |",
    ]
    attribution.extend(
        f"| {repo} | `{commit}` | {licence} | {'yes' if repo in released_repos else 'no'} |"
        for repo, commit, licence in admitted
    )
    (OUT / "ATTRIBUTION.md").write_text("\n".join(attribution) + "\n", encoding="utf-8")
    notice = [
        "Sidq dbt documentation claim dataset",
        "",
        "All pinned, licence-admitted source repositories and commits are listed in ATTRIBUTION.md. Root upstream NOTICE files found in the pinned Apache-2.0 archives follow.",
    ]
    for repo, commit, licence in admitted:
        if licence == "Apache-2.0" and (upstream := sources.get(repo, {}).get("notice")): notice.extend(["", f"--- {repo} @ {commit} ---", upstream])
    (OUT / "NOTICE").write_text("\n".join(notice) + "\n", encoding="utf-8")


def write_datasheet(rows: list[dict[str, Any]], train: list[dict[str, Any]], evaluation: list[dict[str, Any]], held_out: list[str], progress: dict[str, Any], diversity: dict[str, int]) -> None:
    counts = collections.Counter(row["class"] for row in rows)
    train_classes = collections.Counter(row["class"] for row in train)
    eval_classes = collections.Counter(row["class"] for row in evaluation)
    train_types, eval_types, all_types = type_counts(train), type_counts(evaluation), type_counts(rows)
    eval_shortfalls = evaluation_shortfalls(evaluation)
    raw_ratio = diversity["raw_distinct_sentences"] / diversity["raw_rows"] if diversity["raw_rows"] else 0
    capped_ratio = diversity["capped_distinct_sentences"] / diversity["capped_rows"] if diversity["capped_rows"] else 0
    released_ratio = (
        diversity["released_distinct_sentences"] / diversity["released_rows"]
        if diversity["released_rows"]
        else 0
    )
    raw_types = progress.get("per_type_counts", {})
    licence_counts = collections.Counter(
        source.get("licence") for source in progress.get("sources", {}).values()
    )
    released_repos = {row["source"]["repo"] for row in rows}
    lines = [
        "# Datasheet: dbt documentation claim dataset",
        "",
        "## Motivation",
        "",
        "This dataset supports extraction of five executable dbt predicate types from schema-documentation sentences: `not_null`, `unique`, `accepted_values`, `relationships`, and `expression`. It is not intended to infer arbitrary business rules.",
        "",
        "## Composition",
        "",
        f"The released dataset has {len(rows)} pairs from {len(released_repos)} repositories: {counts['positive']} positive, {counts['negative']} negative, and {counts['hard_negative']} hard-negative. The whole-repository split uses deterministic seed `{SPLIT_SEED}` and has {len(train)} train and {len(evaluation)} eval pairs.",
        "",
        "| Split | Positive | Negative | Hard negative | no-claim | not_null | unique | accepted_values | relationships | expression |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| Train | {train_classes['positive']} | {train_classes['negative']} | {train_classes['hard_negative']} | {train_types['no-claim']} | {train_types['not_null']} | {train_types['unique']} | {train_types['accepted_values']} | {train_types['relationships']} | {train_types['expression']} |",
        f"| Eval | {eval_classes['positive']} | {eval_classes['negative']} | {eval_classes['hard_negative']} | {eval_types['no-claim']} | {eval_types['not_null']} | {eval_types['unique']} | {eval_types['accepted_values']} | {eval_types['relationships']} | {eval_types['expression']} |",
        f"| Total | {counts['positive']} | {counts['negative']} | {counts['hard_negative']} | {all_types['no-claim']} | {all_types['not_null']} | {all_types['unique']} | {all_types['accepted_values']} | {all_types['relationships']} | {all_types['expression']} |",
        "",
        "## Evaluation measurability",
        "",
        f"A published per-type accuracy requires at least {EVAL_MIN_PER_TYPE} eval examples for that predicate. The no-claim class requires at least {EVAL_MIN_NO_CLAIM} eval examples. These are minimum reporting thresholds, not substitutes for uncertainty intervals or error analysis.",
        "",
        "| Label | Eval examples | Minimum | Status |",
        "| --- | ---: | ---: | --- |",
        *(f"| {kind} | {eval_types[kind]} | {EVAL_TYPE_FLOORS.get(kind, EVAL_MIN_NO_CLAIM)} | {'measurable' if not eval_shortfalls[kind] else 'not measurable at this sample size (short by ' + str(eval_shortfalls[kind])} |" for kind in (*PREDICATE_TYPES, "no-claim")),
        "",
        f"The raw crawl contains {diversity['raw_rows']} rows, {diversity['raw_distinct_sentences']} distinct sentences ({raw_ratio:.2%}), and {diversity['raw_distinct_sentence_target_pairs']} distinct (sentence, target) pairs. Exact source/test de-duplication leaves {diversity['exact_rows']} rows. The global three-occurrence sentence cap leaves {diversity['capped_rows']} rows, {diversity['capped_distinct_sentences']} distinct sentences ({capped_ratio:.2%}), and {diversity['capped_distinct_sentence_target_pairs']} distinct pairs.",
        "",
        f"After class-balanced selection, the released dataset contains {diversity['released_distinct_sentences']} distinct sentences out of {diversity['released_rows']} rows ({released_ratio:.2%}) and {diversity['released_distinct_sentence_target_pairs']} distinct (sentence, target) pairs. Every released row records the pre-cap sentence `frequency`.",
        "",
        "## Collection process",
        "",
        f"The collector planned {len(progress.get('planned_repos', []))} candidates and attempted {len(progress.get('completed_repos', []))}; {len(progress.get('sources', {}))} pinned archives passed the licence gate and were readable enough to record as admitted sources. It pins archives to exact commits, admits only MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, 0BSD, Unlicense, and CC0-1.0, excludes generated `target/` and installed `dbt_packages/`, and preserves repo/path/commit/licence per pair.",
        "",
        "Deliberate searches covered `accepted_values`, `relationships`, `dbt_utils.expression_is_true`, `dbt_utils.accepted_range`, and `dbt_expectations.expect_column_values_to_*`. Candidate discovery does not bypass the pinned-archive licence gate.",
        "",
        "| Positive predicate | Raw count | Floor | Status |",
        "| --- | ---: | ---: | --- |",
    ]
    for kind in ("accepted_values", "relationships", "expression"):
        floor = RARE_TYPE_FLOORS[kind]
        count = raw_types.get(kind, 0)
        lines.append(f"| {kind} | {count} | {floor} | {'met' if count >= floor else 'short by ' + str(floor - count)} |")
    lines.extend(
        [
            "",
            "Admitted-source licence counts: "
            + ", ".join(f"{licence} {count}" for licence, count in sorted(licence_counts.items()))
            + ".",
            "",
        "## Preprocessing",
        "",
        "Descriptions are sentence-split. Unsupported, malformed, model-level, or non-literal tests are dropped rather than assigned an invented predicate. Exact source/test duplicates are removed, and identical sentence text is capped globally at three rows with deterministic rare-type-first ordering.",
        "",
        f"The final class sampler targets 50% positive, 35% negative, and 15% hard-negative. Within each class and positive predicate floor it cycles deterministically across repositories before taking another row from the same repository, preventing a few large packages from dominating. Eval is selected only from held-out repositories and targets {EVAL_TARGET_ROWS} pairs, with {EVAL_MIN_PER_TYPE} examples of each predicate type and at least {EVAL_MIN_NO_CLAIM} no-claim examples when the pool permits.",
        "",
        "| Release-filtering stage | Before | After | Removed | Reason |",
        "| --- | ---: | ---: | ---: | --- |",
        f"| Exact source/test de-duplication | {diversity['raw_rows']} | {diversity['exact_rows']} | {diversity['raw_rows'] - diversity['exact_rows']} | Repeated identical source/test records |",
        f"| Three-occurrence sentence cap | {diversity['exact_rows']} | {diversity['capped_rows']} | {diversity['exact_rows'] - diversity['capped_rows']} | Prevent auto-generated or repeated wording from dominating |",
        f"| Balanced, cross-repository sampling | {diversity['capped_rows']} | {diversity['released_rows']} | {diversity['capped_rows'] - diversity['released_rows']} | Enforce the release limit, class target, rare-type floors, and whole-repository eval split |",
        "",
        "## Uses",
            "",
            "Suitable for training and evaluating claim-to-executable-predicate extraction in dbt schema documentation. It is not a complete sample of data-quality rules and should not be used to infer semantics absent from a documented adjacent test.",
            "",
        "## Distribution",
        "",
        "Records conform to `schema.json`. `ATTRIBUTION.md` lists every pinned, licence-admitted source repository and commit, including sources absent from train/eval after release filtering. `NOTICE` preserves root upstream NOTICE text found in pinned Apache-2.0 archives.",
            "",
            "## Maintenance",
            "",
            "Use `scripts/mine_dbt_claims.py --resume` after interruption and `--finalize` to regenerate derived files. Re-audit licences and notices before a newly crawled distribution. The Sidq maintainers own updates; no personal or private data removal mechanism is expected because collection is limited to public repository documentation, but upstream removal requests should be reviewed manually.",
            "",
            "### Held-out repositories",
            "",
        ]
    )
    lines.extend(f"- {repo}" for repo in held_out)
    lines.extend(["", "### Dropped tests", ""])
    lines.extend(f"- {reason}: {count}" for reason, count in sorted(progress.get("dropped", {}).items(), key=lambda item: (-item[1], item[0]))) if progress.get("dropped") else lines.append("- None")
    lines.extend(
        [
            "",
            f"Additionally, {len(progress.get('skipped', []))} repository candidates were skipped because they were unavailable, unreadable, or lacked an unambiguous allowed top-level licence; none contributed records.",
            "",
            "### Remaining biases",
            "",
            "- Public dbt schema documentation and generic tests overrepresent `not_null` and `unique`; rare-type floors do not make the corpus representative.",
            "- The corpus is biased toward English-speaking analytics projects that publish YAML publicly.",
            "- The lexical hard-negative heuristic does not prove a statement is uncheckable in every organisation.",
            "- Tests adjacent to descriptions are treated as claims expressed by each sentence, which can over-attach predicates when a description contains multiple sentences.",
            "- Repository hold-out reduces house-style leakage, but forks and related package ecosystems can still share wording and conventions.",
            "",
        ]
    )
    (OUT / "DATASHEET.md").write_text("\n".join(lines), encoding="utf-8")


def load_raw() -> list[dict[str, Any]]:
    rows = []
    for path in sorted(RAW.glob("batch-*.jsonl")): rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Checkpointed miner for licence-audited dbt claim pairs")
    parser.add_argument("--limit", type=int, default=5000, help="maximum released rows")
    parser.add_argument("--repos", nargs="*", help="explicit owner/repo list; only valid when starting")
    parser.add_argument("--repo-count", type=int, default=400, help="discovered candidates to plan (300--800)")
    parser.add_argument("--pause", type=float, default=.15, help="seconds between repositories")
    parser.add_argument("--resume", action="store_true", help="resume from raw/_progress.json")
    parser.add_argument("--expand-package-hub", action="store_true", help="append public dbt package-registry repositories to an existing plan")
    parser.add_argument("--expand-targeted-search", action="store_true", help="append rare-test candidates from public code search to an existing plan")
    parser.add_argument("--expand-github-candidates", action="store_true", help="append candidates persisted from authenticated GitHub code search")
    parser.add_argument("--plan-only", action="store_true", help="persist discovery/plan changes without collecting a batch")
    parser.add_argument("--reset-collection", action="store_true", help="remove only existing raw batches, then start")
    parser.add_argument("--finalize", action="store_true", help="write train/eval/package files from raw batches")
    args = parser.parse_args()
    if args.limit < 1: parser.error("--limit must be positive")
    if not 300 <= args.repo_count <= 800: parser.error("--repo-count must be between 300 and 800")
    OUT.mkdir(parents=True, exist_ok=True); RAW.mkdir(parents=True, exist_ok=True)
    if args.finalize:
        try: progress = reconcile_progress(read_progress())
        except FileNotFoundError as error: parser.error(str(error))
        raw = load_raw()
        if not raw: parser.error("no raw batch records found")
        train, evaluation, held_out, diversity = build_splits(raw, args.limit); released = train + evaluation
        diversity = add_released_diversity(diversity, released)
        write_jsonl(OUT / "train.jsonl", train); write_jsonl(OUT / "eval.jsonl", evaluation); write_schema(); write_attribution(released, progress.get("sources", {})); write_datasheet(released, train, evaluation, held_out, progress, diversity)
        missing = missing_types(train, evaluation)
        eval_shortfalls = evaluation_shortfalls(evaluation)
        raw_types, released_types = type_counts(raw), type_counts(released)
        raw_shortfalls = {kind: max(0, floor - raw_types[kind]) for kind, floor in RARE_TYPE_FLOORS.items()}
        released_shortfalls = {kind: max(0, floor - released_types[kind]) for kind, floor in RARE_TYPE_FLOORS.items()}
        print(json.dumps({
            "total": len(released),
            "train": len(train),
            "eval": len(evaluation),
            "classes": collections.Counter(row["class"] for row in released),
            "types": {"raw": raw_types, "train": type_counts(train), "eval": type_counts(evaluation), "released": released_types},
            "diversity": diversity,
            "repositories": {
                "planned": len(progress.get("planned_repos", [])),
                "attempted": len(progress.get("completed_repos", [])),
                "admitted": len(progress.get("sources", {})),
                "released": len({row["source"]["repo"] for row in released}),
                "held_out": held_out,
            },
            "missing_types": missing,
            "eval_measurement_shortfalls": eval_shortfalls,
            "floor_shortfalls": {"raw": raw_shortfalls, "released": released_shortfalls},
            "split_seed": SPLIT_SEED,
        }, default=dict, sort_keys=True))
        return 2 if any(missing.values()) or any(eval_shortfalls.values()) or any(released_shortfalls.values()) else 0
    if args.reset_collection: reset_collection()
    if args.resume:
        try: progress = reconcile_progress(read_progress())
        except FileNotFoundError as error: parser.error(str(error))
        if args.expand_package_hub and expand_plan(progress, package_hub_repositories(), args.repo_count):
            atomic_json(PROGRESS, progress)
        if args.expand_targeted_search and expand_plan(progress, targeted_search_repositories(), args.repo_count):
            progress.setdefault("discovery", {})["targeted_search_queries"] = list(TARGETED_SEARCH_QUERIES)
            atomic_json(PROGRESS, progress)
        if args.expand_github_candidates and expand_plan(progress, github_candidate_repositories(), args.repo_count):
            progress.setdefault("discovery", {})["github_candidate_file"] = str(GITHUB_CANDIDATES)
            atomic_json(PROGRESS, progress)
        if args.plan_only:
            print(json.dumps({"planned_repos": len(progress["planned_repos"]), "repos_done": progress["repos_done"], "repos_remaining": len(progress["repos_remaining"])}, sort_keys=True))
            return 0
    elif PROGRESS.exists(): parser.error("a collection already exists; use --resume or --reset-collection")
    else:
        repositories = args.repos if args.repos is not None else discover_repositories(args.repo_count)
        repositories = list(dict.fromkeys(repositories))
        if not repositories: parser.error("repository discovery returned no candidates")
        progress = initial_progress(repositories); atomic_json(PROGRESS, progress)
    try:
        acquire_collection_lock()
        collect(progress, args.pause)
    except RuntimeError as error:
        parser.error(str(error))
    finally:
        release_collection_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
