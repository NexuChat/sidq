#!/usr/bin/env python3
"""Discover GitHub repositories with code search, then mine cloned worktrees.

Code search is deliberately used only for repository discovery.  Every source
file is read from a short-lived shallow clone; no blob or contents API calls
are made.  State is committed after each repository so --resume loses, at
most, the repository being cloned when a worker is killed.
"""
from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

RAW = Path("data/claims/raw-v7/error-message-crawl")
# Keep every crawl artefact, including the discovery ledger, in its versioned
# raw directory.  This miner must never alter the shared merge input.
REPOS = RAW / "repos-to-clone.jsonl"
PROGRESS = RAW / "_progress.json"
LOCK = RAW / ".mine.lock"
SCHEMA_VERSION = "7.error-pairs.1"
MIN_FREE_BYTES = 5 * 1024**3
SEARCH_INTERVAL_SECONDS = 6.2  # <= authenticated code-search's ~10/minute bucket
LICENSE_BATCH_SIZE = 25
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Unlicense", "CC0-1.0"}
TYPES = ("accepted_values", "relationships", "unique", "not_null", "expression")

CONSTRAINT_WORDS = {
    "accepted_values": re.compile(r"\b(?:one of|only|allowed values?|valid values?|permitted|must (?:be|match)|choose from|options? (?:are|include))\b", re.IGNORECASE),
    "relationships": re.compile(r"\b(?:reference(?:s)?|foreign key|relat(?:es?|ed|ionship)|belongs to|parent|linked to)\b", re.IGNORECASE),
    "unique": re.compile(r"\b(?:unique|distinct|no duplicates?|one (?:per|of each)|only one)\b", re.IGNORECASE),
    "not_null": re.compile(r"\b(?:required|mandatory|not[ -]?null|cannot be null|must not be null|always (?:provided|present|populated)|must be provided)\b", re.IGNORECASE),
    "expression": re.compile(r"\b(?:at least|at most|minimum|maximum|greater than|less than|between|must match|must start with|must end with|format|pattern|regex|characters?|digits?|length|valid|positive|negative|empty)\b|(?:>=|<=|≥|≤)", re.IGNORECASE),
}
NOT_NULL_NEGATION = re.compile(r"\b(?:optional|not required|may be null|can be null|nullable)\b", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class Candidate:
    kind: str
    column: str
    description: str | None
    evidence: list[Any]
    context: str


class RateLimited(RuntimeError):
    pass


class StopForSpace(RuntimeError):
    pass


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def gh_json(endpoint: str) -> Any:
    result = subprocess.run(["gh", "api", endpoint], check=False, capture_output=True, text=True, timeout=120)
    message = result.stderr.strip()
    if result.returncode and ("rate limit" in message.lower() or "HTTP 429" in message):
        raise RateLimited(message)
    if result.returncode:
        raise RuntimeError(message or f"gh api failed: {endpoint}")
    return json.loads(result.stdout)


def gh_graphql(query: str) -> dict[str, Any]:
    result = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}"], check=False, capture_output=True, text=True, timeout=120)
    message = result.stderr.strip()
    if result.returncode and "rate limit" in message.lower():
        raise RateLimited(message)
    if result.returncode:
        raise RuntimeError(message or "gh graphql failed")
    return json.loads(result.stdout)


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def source_segment(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def values_from(node: ast.AST | None) -> list[Any]:
    value = literal(node)
    if not isinstance(value, (list, tuple, set)):
        return []
    values: list[Any] = []
    for item in value:
        if isinstance(item, (list, tuple)) and item:
            values.append(item[0])
        elif isinstance(item, (str, int, float, bool)) or item is None:
            values.append(item)
    return values


def literal_values(node: ast.AST) -> list[Any]:
    if not isinstance(node, ast.Subscript) or call_name(node.value) != "Literal":
        return []
    members = node.slice.elts if isinstance(node.slice, (ast.Tuple, ast.List)) else [node.slice]
    return [value for member in members if isinstance((value := literal(member)), (str, int, float, bool)) or value is None]


def django_choices(tree: ast.AST, source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        choices = keyword(node, "choices")
        if choices is None:
            continue
        description = literal(keyword(node, "help_text"))
        rows.append(Candidate("accepted_values", "field", description if isinstance(description, str) else None, values_from(choices), source_segment(source, node)))
    return rows


def django_relationships(tree: ast.AST, source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node.func) == "ForeignKey":
            description = literal(keyword(node, "help_text"))
            target = source_segment(source, node.args[0]) if node.args else ""
            rows.append(Candidate("relationships", "field", description if isinstance(description, str) else None, [target], source_segment(source, node)))
    return rows


def pydantic_literal(tree: ast.AST, source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Call) or call_name(node.value.func) != "Field":
            continue
        values = literal_values(node.annotation)
        if not values:
            continue
        description = literal(keyword(node.value, "description"))
        column = node.target.id if isinstance(node.target, ast.Name) else "field"
        rows.append(Candidate("accepted_values", column, description if isinstance(description, str) else None, values, source_segment(source, node)))
    return rows


def sqlalchemy_checks(tree: ast.AST, source: str) -> list[Candidate]:
    checks = [source_segment(source, node) for node in ast.walk(tree) if isinstance(node, ast.Call) and call_name(node.func) == "CheckConstraint"]
    if not checks:
        return []
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or call_name(node.func) not in {"Column", "mapped_column"}:
            continue
        description = literal(keyword(node, "comment"))
        if not isinstance(description, str):
            continue
        column = literal(node.args[0]) if node.args else "field"
        rows.append(Candidate("expression", column if isinstance(column, str) else "field", description, checks, source_segment(source, node)))
    return rows


def zod_enums(source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    # Tolerant enough for chains split across a handful of lines, while the
    # proximity window prevents matching a .describe() from another schema.
    for match in re.finditer(r"(?:z\.)?enum\(\s*\[([^\]]+)\]\s*\)(?P<chain>[\s\S]{0,900}?)\.describe\(\s*(?P<quote>['\"`])(?P<description>.*?)(?P=quote)\s*\)", source):
        values = re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))
        context = match.group(0)
        rows.append(Candidate("accepted_values", "field", re.sub(r"\s+", " ", match.group("description")).strip(), values, context))
    return rows


def sql_checks(source: str) -> list[Candidate]:
    try:
        expressions = sqlglot.parse(source, read="postgres")
    except Exception:
        return []
    check_types = (exp.Check, exp.CheckColumnConstraint)
    checks = [check.sql(dialect="postgres") for expression in expressions for check_type in check_types for check in expression.find_all(check_type)]
    if not checks:
        return []
    rows: list[Candidate] = []
    for expression in expressions:
        for comment in expression.find_all(exp.Comment):
            if str(comment.args.get("kind", "")).upper() != "COLUMN":
                continue
            description = comment.expression.name if isinstance(comment.expression, exp.Literal) else None
            rows.append(Candidate("expression", comment.this.sql(dialect="postgres"), description, checks, comment.sql(dialect="postgres")))
    return rows


def candidates_for(pattern: str, source: str) -> list[Candidate]:
    if pattern.startswith("py_"):
        return python_error_pairs(source)
    if pattern.startswith("ts_"):
        return typescript_error_pairs(source)
    if pattern == "zod":
        return zod_enums(source)
    if pattern == "sql":
        return sql_checks(source)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if pattern == "django_choices":
        return django_choices(tree, source)
    if pattern == "django_relationships":
        return django_relationships(tree, source)
    if pattern == "pydantic_literal":
        return pydantic_literal(tree, source)
    if pattern == "sqlalchemy":
        return sqlalchemy_checks(tree, source)
    return []


def string_argument(node: ast.AST | None) -> str | None:
    value = literal(node)
    return value if isinstance(value, str) else None


def constraint_kind(message: str, values: list[Any]) -> str | None:
    """Classify only messages that assert a usable, nearby constraint."""
    lowered = message.lower()
    if values and re.search(r"\b(?:must be one of|allowed values? (?:are|include)|valid values? (?:are|include)|one of the following|permitted values?)\b", lowered):
        return "accepted_values"
    if CONSTRAINT_WORDS["not_null"].search(message):
        return "not_null"
    if CONSTRAINT_WORDS["relationships"].search(message):
        return "relationships"
    if CONSTRAINT_WORDS["unique"].search(message):
        return "unique"
    if CONSTRAINT_WORDS["expression"].search(message):
        return "expression"
    return None


def literals_in(node: ast.AST | None) -> list[Any]:
    if node is None:
        return []
    values: list[Any] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Constant) and isinstance(item.value, (str, int, float, bool)):
            values.append(item.value)
    # Preserve source order while making an ``in`` test's value list usable.
    return list(dict.fromkeys(values))


def source_window(source: str, node: ast.AST, padding: int = 1) -> str:
    lines = source.splitlines()
    start = max(0, getattr(node, "lineno", 1) - 1 - padding)
    end = min(len(lines), getattr(node, "end_lineno", getattr(node, "lineno", 1)) + padding)
    return "\n".join(lines[start:end]).strip()


def python_error_pairs(source: str) -> list[Candidate]:
    """Pair literal validation messages with their enclosing local check.

    The parent walk deliberately stops at the nearest conditional; this avoids
    treating a message and an unrelated, distant check from the same function
    as a training pair.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    rows: list[Candidate] = []

    def append(message: str | None, check: ast.AST, anchor: ast.AST, values: list[Any] | None = None) -> None:
        if not message:
            return
        stated_values = values if values is not None else literals_in(check)
        kind = constraint_kind(message, stated_values)
        if kind is None:
            return
        evidence: list[Any] = stated_values if kind == "accepted_values" else [source_segment(source, check)]
        rows.append(Candidate(kind, "field", message, evidence, source_window(source, anchor)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            append(string_argument(node.msg), node.test, node)
            continue
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        if call_name(node.exc.func) not in {"ValidationError", "ValueError"}:
            continue
        message = next((string_argument(arg) for arg in node.exc.args if string_argument(arg) is not None), None)
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, (ast.If, ast.Assert, ast.While)):
            parent = parents.get(parent)
        if isinstance(parent, ast.If) or isinstance(parent, ast.Assert):
            append(message, parent.test, node)

    # DRF field declarations keep both their check and error_messages in one
    # call node, so they satisfy the same-locality rule without parent walks.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        messages = keyword(node, "error_messages")
        if not isinstance(messages, ast.Dict):
            continue
        choices = values_from(keyword(node, "choices"))
        check = source_segment(source, node)
        for value in messages.values:
            message = string_argument(value)
            kind = constraint_kind(message or "", choices)
            if kind is None:
                continue
            evidence: list[Any] = choices if kind == "accepted_values" else [check]
            rows.append(Candidate(kind, "field", message, evidence, source_window(source, node, 0)))
    return rows


# Keeping this deliberately quote-agnostic avoids a regex back-reference that
# becomes invalid when embedded inside the named ``check`` capture below.
_QUOTED = r"['\"](?P<message>(?:\\.|[^'\"])*)['\"]"


def js_values(text: str) -> list[Any]:
    values: list[Any] = []
    for item in re.findall(r"['\"]([^'\"]+)['\"]|\b(-?\d+(?:\.\d+)?)\b", text):
        value = item[0] or item[1]
        if value:
            values.append(value)
    return list(dict.fromkeys(values))


def typescript_error_pairs(source: str) -> list[Candidate]:
    """Mine only validator calls whose literal message is in that call chain."""
    rows: list[Candidate] = []
    lines = source.splitlines()
    for index in range(len(lines)):
        # Validator chains commonly wrap once or twice; three lines remains a
        # tight enough window to avoid distant check/message pairing.
        snippet = "\n".join(lines[index:index + 3])
        if len(snippet) > 1800:
            continue
        patterns = (
            ("accepted", rf"(?P<check>\.(?:oneOf|valid)\(\s*\[(?P<values>[^\]]+)\]\s*,\s*{_QUOTED}\s*\))"),
            ("accepted", rf"(?P<check>z\.enum\(\s*\[(?P<values>[^\]]+)\][^)]{{0,350}}?(?:message\s*:\s*)?{_QUOTED}\s*\))"),
            ("generic", rf"(?P<check>\.(?:refine|min|max|length|matches|regex|url|startsWith|endsWith|greater|less)\([\s\S]{{0,500}}?(?:,\s*|message\s*:\s*){_QUOTED}\s*\)?\s*\))"),
            ("joi", rf"(?P<check>\.valid\((?P<values>[^)]*)\)[\s\S]{{0,350}}?\.messages\(\s*\{{[\s\S]{{0,250}}?{_QUOTED}[\s\S]{{0,250}}?\}}\s*\))"),
        )
        for flavour, expression in patterns:
            for match in re.finditer(expression, snippet, re.IGNORECASE):
                message = bytes(match.group("message"), "utf-8").decode("unicode_escape")
                values = js_values(match.groupdict().get("values") or "")
                kind = constraint_kind(message, values)
                if flavour == "accepted" and values:
                    kind = "accepted_values" if constraint_kind(message, values) == "accepted_values" else kind
                if kind is None:
                    continue
                evidence: list[Any] = values if kind == "accepted_values" else [match.group("check")]
                rows.append(Candidate(kind, "field", message, evidence, snippet.strip()))
    return rows


def accepts(candidate: Candidate) -> bool:
    description = candidate.description
    if not description or not CONSTRAINT_WORDS[candidate.kind].search(description):
        return False
    if candidate.kind == "not_null" and NOT_NULL_NEGATION.search(description):
        return False
    if candidate.kind == "accepted_values":
        # A dynamic choices expression is not a usable accepted-values claim:
        # retain only literal enum members that can be put in the training row.
        if not candidate.evidence:
            return False
        lowered = description.lower()
        values = [str(value).lower() for value in candidate.evidence if str(value)]
        # A generic "invalid status" or "must be one of allowed values" is
        # not a usable accepted-values sentence: it does not state the set.
        if not re.search(r"\b(?:must be one of|allowed values? (?:are|include)|valid values? (?:are|include)|one of the following|permitted values?)\b", lowered) or not values or not all(value in lowered for value in values):
            return False
    return True


BASE_QUERIES = {
    # Keep each phrase separately measurable.  The first four are the
    # accepted-values phase; bounds and empty-value messages follow only when
    # that phase has exhausted its partitions.
    "py_must_be_one_of": '"must be one of" ValidationError language:Python NOT is:archived',
    "ts_must_be_one_of": '"must be one of" language:TypeScript NOT is:archived',
    "py_allowed_values_are": '"Allowed values are" language:Python NOT is:archived',
    "py_one_of_following": '"one of the following" ValidationError language:Python NOT is:archived',
    "ts_one_of_following": '"one of the following" language:TypeScript NOT is:archived',
    "py_must_be_at_least": '"must be at least" language:Python NOT is:archived',
    "ts_must_be_at_least": '"must be at least" language:TypeScript NOT is:archived',
    "py_must_be_less_than": '"must be less than" language:Python NOT is:archived',
    "ts_must_be_less_than": '"must be less than" language:TypeScript NOT is:archived',
    "py_must_start_with": '"must start with" language:Python NOT is:archived',
    "ts_must_start_with": '"must start with" language:TypeScript NOT is:archived',
    "py_cannot_be_negative": '"cannot be negative" language:Python NOT is:archived',
    "ts_cannot_be_negative": '"cannot be negative" language:TypeScript NOT is:archived',
    "py_must_not_be_empty": '"must not be empty" language:Python NOT is:archived',
    "ts_must_not_be_empty": '"must not be empty" language:TypeScript NOT is:archived',
}
# GitHub's code-search endpoint supports language and code terms here, but not
# repository ``stars``/``pushed`` ranges.  Fetch the ten code-search pages for
# each independently measured phrase instead of issuing zero-result slices.
AXES = (("phrase", ("",)),)
PRIORITY_PATTERNS = (
    "py_must_be_one_of", "ts_must_be_one_of", "py_allowed_values_are", "py_one_of_following", "ts_one_of_following",
)
OTHER_PATTERNS = tuple(pattern for pattern in BASE_QUERIES if pattern not in PRIORITY_PATTERNS)


def query_plan() -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for pattern in (*PRIORITY_PATTERNS, *OTHER_PATTERNS):
        for axis, values in AXES:
            for number, qualifier in enumerate(values, 1):
                plan.append({"id": f"{pattern}-{axis}-{number}", "pattern": pattern, "query": f"{BASE_QUERIES[pattern]} {qualifier}", "axis": axis, "status": "pending", "page": 1})
    return plan


def blank_stats() -> dict[str, int]:
    return {"files_scanned": 0, "candidates": 0, "missing_description": 0, "rejected": 0, "pairs_extracted": 0}


def initial_progress() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": now(),
        "partitions": query_plan(),
        "repos": {},
        "sentence_counts": {},
        "totals": {"repos_discovered": 0, "repos_licenced": 0, "repos_skipped_licence": 0, "repos_cloned": 0, "repos_clone_failed": 0, **blank_stats(), "by_pattern": {}},
    }


def add_stats(target: dict[str, Any], current: dict[str, int]) -> None:
    for key, value in current.items():
        target[key] = target.get(key, 0) + value


def rate(stats: dict[str, Any]) -> float:
    return round(100 * stats.get("pairs_extracted", 0) / stats["candidates"], 2) if stats.get("candidates") else 0.0


def refresh_repos(progress: dict[str, Any]) -> None:
    rows = []
    for name, repo in sorted(progress["repos"].items()):
        rows.append({"full_name": name, "licence": repo.get("licence"), "licence_allowed": repo.get("licence_allowed", False), "patterns": sorted(repo["patterns"]), "status": repo["status"], "commit": repo.get("commit"), "discovered_by": sorted(repo["partitions"])})
    write_jsonl(REPOS, rows)


def refresh_report(progress: dict[str, Any]) -> None:
    totals = progress["totals"]
    atomic_json(RAW / "report.json", {
        "generated_at": now(),
        **{key: totals[key] for key in ("repos_discovered", "repos_licenced", "repos_skipped_licence", "repos_cloned", "repos_clone_failed", "files_scanned", "candidates", "pairs_extracted")},
        "overall_survival_rate_percent": rate(totals),
        "survival_by_pattern": {name: {**stats, "survival_rate_percent": rate(stats)} for name, stats in sorted(totals["by_pattern"].items())},
        "partitions": [{key: partition.get(key) for key in ("id", "pattern", "axis", "status", "page", "search_total_count")} for partition in progress["partitions"]],
    })


def sleep_for_search(last_search: float) -> float:
    elapsed = time.monotonic() - last_search
    if elapsed < SEARCH_INTERVAL_SECONDS:
        time.sleep(SEARCH_INTERVAL_SECONDS - elapsed)
    return time.monotonic()


def license_repositories(names: list[str], progress: dict[str, Any]) -> None:
    for offset in range(0, len(names), LICENSE_BATCH_SIZE):
        batch = names[offset:offset + LICENSE_BATCH_SIZE]
        fields = []
        for index, name in enumerate(batch):
            owner, repo = name.split("/", 1)
            fields.append(f'r{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) {{ nameWithOwner licenseInfo {{ spdxId }} defaultBranchRef {{ name target {{ ... on Commit {{ oid }} }} }} }}')
        while True:
            try:
                payload = gh_graphql("query { " + " ".join(fields) + " }")
                break
            except RateLimited as error:
                print(f"GitHub rate limited during licence lookup; sleeping 75s: {error}", file=sys.stderr, flush=True)
                time.sleep(75)
        data = payload.get("data", {})
        for index, name in enumerate(batch):
            repository = data.get(f"r{index}")
            entry = progress["repos"][name]
            licence = ((repository or {}).get("licenseInfo") or {}).get("spdxId")
            entry["licence"] = licence
            entry["licence_allowed"] = licence in ALLOWED_LICENSES
            branch = (repository or {}).get("defaultBranchRef") or {}
            entry["branch"] = branch.get("name")
            entry["commit"] = ((branch.get("target") or {}).get("oid"))
            if entry["licence_allowed"]:
                entry["status"] = "pending"
                progress["totals"]["repos_licenced"] += 1
            else:
                entry["status"] = "skipped_licence"
                progress["totals"]["repos_skipped_licence"] += 1
        atomic_json(PROGRESS, progress)
        refresh_repos(progress)
        refresh_report(progress)


def discover_partition(partition: dict[str, Any], progress: dict[str, Any], pages_per_partition: int, last_search: float) -> float:
    for _ in range(pages_per_partition):
        if partition["page"] > 10:
            partition["status"] = "complete"
            break
        last_search = sleep_for_search(last_search)
        endpoint = "search/code?" + urllib.parse.urlencode({"q": partition["query"], "per_page": 100, "page": partition["page"]})
        while True:
            try:
                payload = gh_json(endpoint)
                break
            except RateLimited as error:
                print(f"GitHub code search rate limited; sleeping 75s: {error}", file=sys.stderr, flush=True)
                time.sleep(75)
                last_search = time.monotonic()
        items = payload.get("items", [])
        partition["search_total_count"] = payload.get("total_count", 0)
        new_names: list[str] = []
        for item in items:
            name = item["repository"]["full_name"]
            repo = progress["repos"].get(name)
            if repo is None:
                progress["repos"][name] = {"patterns": [partition["pattern"]], "completed_patterns": [], "partitions": [partition["id"]], "status": "awaiting_licence"}
                progress["totals"]["repos_discovered"] += 1
                new_names.append(name)
            else:
                repo["patterns"] = sorted(set(repo["patterns"]) | {partition["pattern"]})
                repo["partitions"] = sorted(set(repo["partitions"]) | {partition["id"]})
                if repo["status"] == "complete" and partition["pattern"] not in repo.get("completed_patterns", []):
                    repo["status"] = "pending"
        partition["page"] += 1
        if len(items) < 100 or partition["page"] > 10:
            partition["status"] = "complete"
        atomic_json(PROGRESS, progress)
        if new_names:
            license_repositories(new_names, progress)
        refresh_repos(progress)
        refresh_report(progress)
        print(json.dumps({"discovery_partition": partition["id"], "page": partition["page"] - 1, "repos_discovered": len(new_names)}, sort_keys=True), flush=True)
        if partition["status"] == "complete":
            break
    return last_search


def allowed_suffixes(patterns: Iterable[str]) -> tuple[str, ...]:
    suffixes: set[str] = set()
    for pattern in patterns:
        suffixes.update((".ts", ".tsx", ".js", ".jsx") if pattern.startswith("ts_") else (".py",))
    return tuple(sorted(suffixes))


def clone_repo(name: str, patterns: list[str]) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    tempdir = tempfile.TemporaryDirectory(prefix="sidq-mine-")
    destination = Path(tempdir.name) / "repo"
    clone = subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", f"https://github.com/{name}.git", str(destination)], capture_output=True, text=True, timeout=240)
    if clone.returncode:
        tempdir.cleanup()
        raise RuntimeError(clone.stderr.strip() or "git clone failed")
    patterns_for_git = []
    for suffix in allowed_suffixes(patterns):
        patterns_for_git.extend((f"*{suffix}", f"**/*{suffix}"))
    setup = subprocess.run(["git", "-C", str(destination), "sparse-checkout", "set", "--no-cone", *patterns_for_git], capture_output=True, text=True, timeout=180)
    if setup.returncode:
        tempdir.cleanup()
        raise RuntimeError(setup.stderr.strip() or "sparse checkout failed")
    checkout = subprocess.run(["git", "-C", str(destination), "checkout"], capture_output=True, text=True, timeout=180)
    if checkout.returncode:
        tempdir.cleanup()
        raise RuntimeError(checkout.stderr.strip() or "sparse checkout failed")
    return destination, tempdir


def make_record(candidate: Candidate, repo: str, licence: str, commit: str, path: Path, pattern: str) -> dict[str, Any]:
    claim: dict[str, Any] = {"type": candidate.kind, "column": candidate.column}
    if candidate.kind == "accepted_values":
        claim["values"] = candidate.evidence
    elif candidate.kind in {"relationships", "expression"}:
        claim["expr"] = "; ".join(map(str, candidate.evidence))
    return {"class": "positive", "input": {"sentence": candidate.description, "column_name": candidate.column, "table_name": str(path), "schema_context": candidate.context[:2000]}, "target": {"claim": claim}, "source_kind": "github_clone", "source_document": str(path), "source": {"repository": repo, "path": str(path), "commit": commit, "licence": licence, "pattern": pattern, "adjacent_check": candidate.context[:2000]}}


def harvest_repo(name: str, entry: dict[str, Any], progress: dict[str, Any]) -> None:
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < MIN_FREE_BYTES:
        raise StopForSpace(f"free space is {free // 1024**3} GB; stopping before clone (minimum is 5 GB)")
    patterns = [pattern for pattern in entry["patterns"] if pattern not in entry.get("completed_patterns", [])]
    if not patterns:
        entry["status"] = "complete"
        return
    first_clone = not entry.get("batches")
    try:
        worktree, temporary = clone_repo(name, patterns)
    except Exception as error:
        entry["status"] = "clone_failed"
        entry["error"] = str(error)[:1000]
        progress["totals"]["repos_clone_failed"] += 1
        atomic_json(PROGRESS, progress); refresh_repos(progress); refresh_report(progress)
        print(f"clone failed {name}: {error}", file=sys.stderr, flush=True)
        return
    try:
        commit_result = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=30)
        commit = commit_result.stdout.strip()
        stats_by_pattern = {pattern: blank_stats() for pattern in patterns}
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        suffixes = allowed_suffixes(patterns)
        for file in worktree.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in suffixes or ".git" in file.parts or file.stat().st_size > 2_000_000:
                continue
            try:
                source = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = file.relative_to(worktree)
            for pattern in patterns:
                if pattern.startswith("ts_") and file.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
                    continue
                if pattern.startswith("py_") and file.suffix.lower() != ".py":
                    continue
                stats = stats_by_pattern[pattern]
                stats["files_scanned"] += 1
                for candidate in candidates_for(pattern, source):
                    stats["candidates"] += 1
                    if candidate.description is None:
                        stats["missing_description"] += 1
                        continue
                    if not accepts(candidate):
                        stats["rejected"] += 1
                        continue
                    fingerprint = hashlib.sha256("\0".join((name, str(relative), pattern, candidate.kind, candidate.description, repr(candidate.evidence))).encode()).hexdigest()
                    sentence_key = re.sub(r"\s+", " ", candidate.description.strip()).casefold()
                    if fingerprint in seen or progress.setdefault("sentence_counts", {}).get(sentence_key, 0) >= 3:
                        continue
                    seen.add(fingerprint)
                    records.append(make_record(candidate, name, entry["licence"], commit, relative, pattern))
                    progress["sentence_counts"][sentence_key] = progress["sentence_counts"].get(sentence_key, 0) + 1
                    stats["pairs_extracted"] += 1
        batch = RAW / f"repo-{hashlib.sha256(name.encode()).hexdigest()[:16]}-{len(entry.get('completed_patterns', [])) + 1:02d}"
        write_jsonl(batch.with_suffix(".jsonl"), records)
        atomic_json(batch.with_suffix(".manifest.json"), {"repository": name, "licence": entry["licence"], "commit": commit, "patterns": patterns, "stats_by_pattern": stats_by_pattern, "rows": len(records), "completed_at": now()})
        entry["completed_patterns"] = sorted(set(entry.get("completed_patterns", [])) | set(patterns))
        entry.setdefault("batches", []).append(batch.name)
        entry.update({"status": "complete", "commit": commit, "completed_at": now()})
        if first_clone:
            progress["totals"]["repos_cloned"] += 1
        for pattern, stats in stats_by_pattern.items():
            target = progress["totals"]["by_pattern"].setdefault(pattern, blank_stats())
            add_stats(target, stats)
            add_stats(progress["totals"], stats)
        atomic_json(PROGRESS, progress); refresh_repos(progress); refresh_report(progress)
        print(json.dumps({"repository": name, "files_scanned": sum(item["files_scanned"] for item in stats_by_pattern.values()), "pairs_extracted": len(records), "patterns": patterns}, sort_keys=True), flush=True)
    finally:
        temporary.cleanup()


def harvest_ready(progress: dict[str, Any], only_patterns: set[str] | None = None, max_repos: int = 3000) -> None:
    for name, entry in progress["repos"].items():
        if progress["totals"]["repos_cloned"] >= max_repos:
            return
        if entry["status"] != "pending":
            continue
        if only_patterns is not None and not set(entry["patterns"]).intersection(only_patterns):
            continue
        harvest_repo(name, entry, progress)


def verify_progress(progress: dict[str, Any]) -> None:
    if progress.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"incompatible progress schema {progress.get('schema_version')}; start a fresh raw-v5 directory")
    for name, entry in progress["repos"].items():
        if entry["status"] == "complete":
            for batch_name in entry.get("batches", []):
                batch = RAW / batch_name
                if not batch.with_suffix(".jsonl").exists() or not batch.with_suffix(".manifest.json").exists():
                    raise RuntimeError(f"checkpoint missing for {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="continue durable discovery and per-repository harvest checkpoints")
    parser.add_argument("--pages-per-partition", type=int, default=1, choices=range(1, 11), help="code-search pages to fetch before moving to the next partition (default: 1)")
    parser.add_argument("--max-repos", type=int, default=3000, choices=range(1500, 3001), help="maximum permissively licensed repositories to clone (default: 3000)")
    args = parser.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            owner = int(LOCK.read_text(encoding="ascii").strip()); os.kill(owner, 0)
        except (ValueError, ProcessLookupError):
            LOCK.unlink()
        else:
            raise SystemExit(f"another miner owns {LOCK} (pid {owner})")
    LOCK.write_text(str(os.getpid()), encoding="ascii")
    try:
        if PROGRESS.exists():
            if not args.resume:
                raise SystemExit(f"{PROGRESS} exists; use --resume")
            progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
        else:
            progress = initial_progress(); atomic_json(PROGRESS, progress)
        verify_progress(progress); refresh_repos(progress); refresh_report(progress)
        last_search = 0.0
        # Keep the accepted-values seams ahead of every lower-priority seam,
        # but round-robin within a phase.  A large Django partition therefore
        # cannot delay the first Pydantic survival-rate measurement for hours.
        for pattern_order in (PRIORITY_PATTERNS, OTHER_PATTERNS):
            while True:
                made_progress = False
                for pattern in pattern_order:
                    partition = next((item for item in progress["partitions"] if item["pattern"] == pattern and item["status"] != "complete"), None)
                    if partition is None:
                        continue
                    made_progress = True
                    last_search = discover_partition(partition, progress, args.pages_per_partition, last_search)
                    # Keep search as discovery only: as soon as a repository
                    # is licence-approved, read it locally and checkpoint it.
                    harvest_ready(progress, {pattern}, args.max_repos)
                    if progress["totals"]["repos_cloned"] >= args.max_repos:
                        print(f"reached requested repository cap ({args.max_repos})", flush=True)
                        return 0
                if not made_progress:
                    break
        return 0
    except StopForSpace as error:
        print(str(error), file=sys.stderr, flush=True)
        return 2
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
