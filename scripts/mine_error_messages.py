#!/usr/bin/env python3
"""Mine constraint-bearing validation messages and codebooks from Git clones.

Discovery uses GitHub code search only to find repositories.  Source is always
read from a shallow, sparse clone and every emitted pair keeps repository,
commit, licence, path, message, and adjacent enforcing check.  A checkpoint is
written after every repository so ``--resume`` is safe after interruption.
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

RAW = Path("data/claims/raw-v6")
PROGRESS = RAW / "_progress.json"
REPOS = RAW / "repos-to-clone.jsonl"
LOCK = RAW / ".mine.lock"
SCHEMA_VERSION = "6.errors.1"
MIN_FREE_BYTES = 5 * 1024**3
SEARCH_INTERVAL_SECONDS = 6.2
LICENSE_BATCH_SIZE = 25
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "0BSD", "Unlicense", "CC0-1.0"}


@dataclasses.dataclass(frozen=True)
class Candidate:
    pattern: str
    kind: str
    column: str
    message: str | None
    check: str
    values: list[Any]
    context: str
    line: int


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


def source_segment(source: str, node: ast.AST) -> str:
    return (ast.get_source_segment(source, node) or "").strip()


def literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def message_from(node: ast.AST | None) -> str | None:
    value = literal(node)
    if isinstance(value, str):
        return value
    # Preserve a validation template when its only dynamic parts are f-string
    # substitutions.  The adjacent AST test is still the label; this merely
    # permits messages such as ``f"amount must be positive: {amount}"``.
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append("{…}")
            else:
                return None
        return "".join(parts)
    return None


def keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def values_from(node: ast.AST | None) -> list[Any]:
    # Keep source order for enum-like set/list/tuple literals.  ``literal_eval``
    # turns a set into an unordered Python set, which would make output flaky.
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values: list[Any] = []
        for item in node.elts:
            value = literal(item)
            if isinstance(value, (str, int, float, bool)) or value is None:
                values.append(value)
            elif isinstance(value, (list, tuple)) and value:
                values.append(value[0])
        return values
    value = literal(node)
    if not isinstance(value, (list, tuple, set, dict)):
        return []
    if isinstance(value, dict):
        return list(value)
    result: list[Any] = []
    for item in value:
        if isinstance(item, (list, tuple)) and item:
            result.append(item[0])
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result.append(item)
    return result


def names_in(text: str) -> str:
    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", text)
    return match.group(1) if match else "field"


def values_in_check(test: ast.AST) -> list[Any]:
    if isinstance(test, ast.Compare):
        for comparator in test.comparators:
            result = values_from(comparator)
            if result:
                return result
    return []


def classify(message: str, check: str, values: list[Any]) -> str:
    lowered = message.lower()
    if values or re.search(r"\b(?:one of|allowed values?|valid values?|permitted|choices?)\b", lowered):
        return "accepted_values"
    if re.search(r"\b(?:required|not null|cannot be null|must be present)\b", lowered) and re.search(r"\bnot\s+\w+|is\s+none|==\s*none", check, re.IGNORECASE):
        return "not_null"
    return "expression"


def candidate(pattern: str, message: str | None, check: str, source: str, line: int, values: list[Any] | None = None, column: str | None = None) -> Candidate:
    values = values or []
    return Candidate(pattern, classify(message or "", check, values), column or names_in(check), message, check, values, source[:2000], line)


def error_message(call: ast.Call) -> str | None:
    if call_name(call.func) not in {"ValidationError", "ValueError", "AssertionError"}:
        return None
    return message_from(call.args[0] if call.args else keyword(call, "message"))


def raise_from(statement: ast.stmt) -> tuple[str | None, ast.Call | None]:
    if not isinstance(statement, ast.Raise) or not isinstance(statement.exc, ast.Call):
        return None, None
    return error_message(statement.exc), statement.exc


def python_if_raises(tree: ast.AST, source: str, pattern: str, only_validator: bool = False) -> list[Candidate]:
    rows: list[Candidate] = []
    validator_lines: set[int] = set()
    if only_validator:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(call_name(item.func) in {"field_validator", "validator"} for item in node.decorator_list if isinstance(item, ast.Call)):
                validator_lines.update(range(node.lineno, getattr(node, "end_lineno", node.lineno) + 1))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not node.body:
            continue
        if only_validator and node.lineno not in validator_lines:
            continue
        message, _ = raise_from(node.body[0])
        if message is None:
            continue
        check = source_segment(source, node.test)
        rows.append(candidate(pattern, message, check, source_segment(source, node), node.lineno, values_in_check(node.test)))
    return rows


def python_asserts(tree: ast.AST, source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            message = message_from(node.msg)
            if message:
                check = source_segment(source, node.test)
                rows.append(candidate("python_assert", message, check, source_segment(source, node), node.lineno, values_in_check(node.test)))
    return rows


def python_error_messages(tree: ast.AST, source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        errors = literal(keyword(node, "error_messages"))
        if not isinstance(errors, dict):
            continue
        choices = values_from(keyword(node, "choices"))
        checks: list[tuple[str, list[Any]]] = []
        if choices:
            checks.append((f"choices={source_segment(source, keyword(node, 'choices'))}", choices))
        for name in ("min_length", "max_length", "min_value", "max_value", "required", "allow_null"):
            value = keyword(node, name)
            if value is not None:
                checks.append((f"{name}={source_segment(source, value)}", []))
        for key, value in errors.items():
            if not isinstance(value, str):
                continue
            matching = [item for item in checks if (key in {"invalid_choice", "invalid"} and item[1]) or key.replace("_", "") in item[0].replace("_", "")]
            for check, values in matching:
                rows.append(candidate("python_error_messages", value, check, source_segment(source, node), node.lineno, values))
    return rows


def python_candidates(source: str) -> list[Candidate]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return (python_if_raises(tree, source, "python_if_raise") + python_asserts(tree, source) +
            python_error_messages(tree, source) + python_if_raises(tree, source, "python_pydantic_validator", only_validator=True))


def quoted_values(text: str) -> list[str]:
    return re.findall(r"['\"]([^'\"]+)['\"]", text)


def closing_paren(source: str, opening: int) -> int | None:
    """Return the end of one JS/TS call without allowing a regex to drift.

    This deliberately recognizes only lexical strings and bracket depth.  It is
    enough for call boundaries, and importantly means that a ``.refine``
    message can never be borrowed from the next schema member.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in "'\"`":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def split_js_arguments(text: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in "'\"`":
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append(text[start:index].strip())
            start = index + 1
    arguments.append(text[start:].strip())
    return arguments


def js_string(text: str) -> str | None:
    text = text.strip()
    if len(text) < 2 or text[0] != text[-1] or text[0] not in "'\"`":
        return None
    if text[0] == "`":
        return None if "${" in text else text[1:-1]
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def ts_candidates(source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for match in re.finditer(r"\.(refine|min|max|length|oneOf)\s*\(", source):
        method = match.group(1)
        opening = match.end() - 1
        closing = closing_paren(source, opening)
        if closing is None:
            continue
        call = source[match.start():closing + 1]
        # A call is one syntactic node; the limit rejects generated/minified
        # blobs while preserving normal multi-line validator callbacks.
        if len(call) > 5_000 or call.count("\n") > 40:
            continue
        arguments = split_js_arguments(source[opening + 1:closing])
        message = js_string(arguments[1]) if len(arguments) >= 2 else None
        line = source.count("\n", 0, match.start()) + 1
        check = re.sub(r"\s+", " ", call).strip()
        # Do not guess the framework from the method name: both Zod and Yup
        # expose ``min``.  The closest constructor in the current expression
        # must identify the framework, otherwise this is not an accepted pair.
        prefix = source[max(0, match.start() - 1_000):match.start()]
        zod_at = max(prefix.rfind("z."), prefix.rfind("zod."))
        yup_at = prefix.rfind("yup.")
        framework = "zod" if zod_at > yup_at else "yup" if yup_at > zod_at else None
        if method == "refine" and message is not None and framework == "zod":
            rows.append(candidate("zod_refine", message, check, call, line))
        elif method in {"min", "max", "length"} and message is not None:
            if framework == "zod":
                rows.append(candidate("zod_bounds", message, check, call, line))
            elif framework == "yup":
                rows.append(candidate("yup_bounds", message, check, call, line))
        elif method == "oneOf" and message is not None and framework == "yup":
            rows.append(candidate("yup_oneof", message, check, call, line, quoted_values(arguments[0])))
    # Joi puts the human message in a chained ``.messages`` call.  Restrict
    # pairing to the same semicolon-delimited expression and require an actual
    # adjacent Joi constraint method in that expression.
    for match in re.finditer(r"\.messages\s*\(", source):
        opening = match.end() - 1
        closing = closing_paren(source, opening)
        if closing is None:
            continue
        start = max(source.rfind(";", 0, match.start()), source.rfind("\n\n", 0, match.start())) + 1
        chain = source[start:closing + 1]
        if len(chain) > 5_000 or chain.count("\n") > 40:
            continue
        constraint = re.search(r"\.(valid|allow|pattern|regex|greater|less|length|min|max)\s*\(", chain)
        if constraint is None:
            continue
        constraint_open = start + constraint.end() - 1
        constraint_close = closing_paren(source, constraint_open)
        if constraint_close is None or constraint_close >= match.start():
            continue
        values = quoted_values(source[constraint_open + 1:constraint_close])
        for message_match in re.finditer(r"(?:'[^']+'|\"[^\"]+\")\s*:\s*(?P<value>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")", source[opening + 1:closing]):
            message = js_string(message_match.group("value"))
            if message is not None:
                line = source.count("\n", 0, match.start()) + 1
                rows.append(candidate("joi_messages", message, re.sub(r"\s+", " ", chain).strip(), chain, line, values))
    return rows


def json_candidates(source: str) -> list[Candidate]:
    try:
        document = json.loads(source)
    except json.JSONDecodeError:
        return []
    rows: list[Candidate] = []
    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            message = value.get("errorMessage")
            if isinstance(message, dict):
                message = next((item for item in message.values() if isinstance(item, str)), None)
            if isinstance(message, str):
                if "enum" in value:
                    rows.append(candidate("json_schema_error_message", message, f"enum={value['enum']!r}", json.dumps(value), 1, list(value["enum"]), path))
                elif "pattern" in value:
                    rows.append(candidate("json_schema_error_message", message, f"pattern={value['pattern']!r}", json.dumps(value), 1, [], path))
                elif any(key in value for key in ("minimum", "maximum", "minLength", "maxLength")):
                    check = ", ".join(f"{key}={value[key]!r}" for key in ("minimum", "maximum", "minLength", "maxLength") if key in value)
                    rows.append(candidate("json_schema_error_message", message, check, json.dumps(value), 1, [], path))
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
    visit(document, "$")
    return rows


def codebook_candidates(source: str) -> list[Candidate]:
    rows: list[Candidate] = []
    for line_number, line in enumerate(source.splitlines(), 1):
        # Handles the terse forms used in public codebooks: GENDER 1=Male;
        # 2=Female; 9=Unknown.  Three labels avoids ordinary key/value data.
        pairs = re.findall(r"(?:^|[;,|\t ]+)['\"]?([A-Za-z0-9._-]+)['\"]?\s*=\s*['\"]?([^;,|\t]+?)['\"]?(?=(?:[;,|\t ]+['\"]?[A-Za-z0-9._-]+['\"]?\s*=)|$)", line)
        if len(pairs) < 3:
            continue
        field = line.split()[0].strip(" ,;|\t")
        values = [value for value, _ in pairs]
        # Keep the source's code/value mapping as the input.  It is itself the
        # constraint statement; inventing a synthetic "must be one of" sentence
        # would break the provenance contract.
        rows.append(Candidate("codebook_inline", "accepted_values", field, line.strip(), line.strip(), values, line.strip(), line_number))
    return rows


def candidates_for(pattern: str, source: str) -> list[Candidate]:
    if pattern.startswith("python_"):
        return [row for row in python_candidates(source) if row.pattern == pattern]
    if pattern in {"zod_refine", "zod_bounds", "yup_oneof", "yup_bounds", "joi_messages"}:
        return [row for row in ts_candidates(source) if row.pattern == pattern]
    if pattern == "json_schema_error_message":
        return json_candidates(source)
    if pattern == "codebook_inline":
        return codebook_candidates(source)
    return []


MESSAGE_RULES = {
    "accepted_values": re.compile(r"\b(?:one of|allowed values?|valid values?|permitted|choices?)\b", re.IGNORECASE),
    "not_null": re.compile(r"\b(?:required|not[ -]?null|cannot be null|must be present)\b", re.IGNORECASE),
    "expression": re.compile(r"\b(?:must|at least|at most|minimum|maximum|greater than|less than|positive|negative|between|exactly|length|characters?|digits?|match|format|pattern)\b|(?:>=|<=|≥|≤)", re.IGNORECASE),
}


def accepts(row: Candidate) -> bool:
    if not row.message or not row.check:
        return False
    if row.pattern == "codebook_inline":
        return len(row.values) >= 3
    if not MESSAGE_RULES[row.kind].search(row.message):
        return False
    if row.kind == "accepted_values" and not row.values:
        return False
    return True


BASE_QUERIES = {
    "python_if_raise": 'raise ValueError language:Python NOT is:archived',
    "python_assert": 'assert language:Python NOT is:archived',
    "python_error_messages": 'error_messages language:Python NOT is:archived',
    "python_pydantic_validator": 'field_validator language:Python NOT is:archived',
    "zod_refine": '.refine language:TypeScript NOT is:archived',
    "zod_bounds": '.min language:TypeScript NOT is:archived',
    "yup_oneof": '.oneOf language:TypeScript NOT is:archived',
    "yup_bounds": 'yup .min language:TypeScript NOT is:archived',
    "joi_messages": '.messages language:TypeScript NOT is:archived',
    "json_schema_error_message": 'errorMessage extension:json NOT is:archived',
    "codebook_inline": 'codebook extension:csv NOT is:archived',
}
SUFFIXES = {
    "python_if_raise": (".py",), "python_assert": (".py",), "python_error_messages": (".py",), "python_pydantic_validator": (".py",),
    "zod_refine": (".ts", ".tsx", ".js", ".jsx"), "zod_bounds": (".ts", ".tsx", ".js", ".jsx"), "yup_oneof": (".ts", ".tsx", ".js", ".jsx"), "yup_bounds": (".ts", ".tsx", ".js", ".jsx"), "joi_messages": (".ts", ".tsx", ".js", ".jsx"),
    "json_schema_error_message": (".json",), "codebook_inline": (".csv", ".tsv", ".txt"),
}
# GitHub's code-search endpoint does not support repository star ranges: it
# silently returns zero rows for e.g. ``stars:0..99``.  Page through each code
# query instead; discovery diversity is handled by the pattern families.
AXES = ("",)


def query_plan() -> list[dict[str, Any]]:
    return [{"id": f"{pattern}-{index}", "pattern": pattern, "query": f"{query} {axis}", "status": "pending", "page": 1} for pattern, query in BASE_QUERIES.items() for index, axis in enumerate(AXES, 1)]


def blank_stats() -> dict[str, int]:
    return {"files_scanned": 0, "candidates": 0, "missing_message": 0, "rejected": 0, "pairs_extracted": 0}


def initial_progress() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "created_at": now(), "partitions": query_plan(), "repos": {}, "totals": {"repos_discovered": 0, "repos_licenced": 0, "repos_skipped_licence": 0, "repos_cloned": 0, "repos_clone_failed": 0, **blank_stats(), "by_pattern": {}}}


def add_stats(target: dict[str, Any], current: dict[str, int]) -> None:
    for key, value in current.items():
        target[key] = target.get(key, 0) + value


def rate(stats: dict[str, Any]) -> float:
    return round(100 * stats.get("pairs_extracted", 0) / stats["candidates"], 2) if stats.get("candidates") else 0.0


def refresh(progress: dict[str, Any]) -> None:
    rows = [{"full_name": name, "licence": entry.get("licence"), "licence_allowed": entry.get("licence_allowed", False), "patterns": sorted(entry["patterns"]), "status": entry["status"], "commit": entry.get("commit")} for name, entry in sorted(progress["repos"].items())]
    write_jsonl(REPOS, rows)
    totals = progress["totals"]
    atomic_json(RAW / "report.json", {"generated_at": now(), **{key: totals[key] for key in ("repos_discovered", "repos_licenced", "repos_skipped_licence", "repos_cloned", "repos_clone_failed", "files_scanned", "candidates", "pairs_extracted")}, "overall_survival_rate_percent": rate(totals), "survival_by_pattern": {name: {**stats, "survival_rate_percent": rate(stats)} for name, stats in sorted(totals["by_pattern"].items())}, "partitions": [{key: part.get(key) for key in ("id", "pattern", "status", "page", "search_total_count")} for part in progress["partitions"]]})


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
                print(f"GitHub rate limited during licence lookup; sleeping 75s: {error}", file=sys.stderr, flush=True); time.sleep(75)
        for index, name in enumerate(batch):
            repository = payload.get("data", {}).get(f"r{index}") or {}
            entry = progress["repos"][name]
            licence = (repository.get("licenseInfo") or {}).get("spdxId")
            entry.update({"licence": licence, "licence_allowed": licence in ALLOWED_LICENSES, "branch": (repository.get("defaultBranchRef") or {}).get("name")})
            if entry["licence_allowed"]:
                entry["status"] = "pending"; progress["totals"]["repos_licenced"] += 1
            else:
                entry["status"] = "skipped_licence"; progress["totals"]["repos_skipped_licence"] += 1
        atomic_json(PROGRESS, progress); refresh(progress)


def discover(partition: dict[str, Any], progress: dict[str, Any], pages: int, last_search: float) -> float:
    for _ in range(pages):
        elapsed = time.monotonic() - last_search
        if elapsed < SEARCH_INTERVAL_SECONDS:
            time.sleep(SEARCH_INTERVAL_SECONDS - elapsed)
        endpoint = "search/code?" + urllib.parse.urlencode({"q": partition["query"], "per_page": 100, "page": partition["page"]})
        while True:
            try:
                payload = gh_json(endpoint); break
            except RateLimited as error:
                print(f"GitHub code search rate limited; sleeping 75s: {error}", file=sys.stderr, flush=True); time.sleep(75)
        last_search = time.monotonic(); items = payload.get("items", []); partition["search_total_count"] = payload.get("total_count", 0)
        names: list[str] = []
        for item in items:
            name = item["repository"]["full_name"]
            entry = progress["repos"].get(name)
            if entry is None:
                progress["repos"][name] = {"patterns": [partition["pattern"]], "completed_patterns": [], "status": "awaiting_licence"}; progress["totals"]["repos_discovered"] += 1; names.append(name)
            else:
                entry["patterns"] = sorted(set(entry["patterns"]) | {partition["pattern"]})
                if entry["status"] == "complete" and partition["pattern"] not in entry.get("completed_patterns", []): entry["status"] = "pending"
        partition["page"] += 1
        if len(items) < 100 or partition["page"] > 10: partition["status"] = "complete"
        atomic_json(PROGRESS, progress)
        if names: license_repositories(names, progress)
        refresh(progress)
        print(json.dumps({"discovery_partition": partition["id"], "repos_discovered": len(names)}, sort_keys=True), flush=True)
        if partition["status"] == "complete": break
    return last_search


def clone_repo(name: str, patterns: list[str]) -> tuple[Path, tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="sidq-mine-errors-"); destination = Path(temporary.name) / "repo"
    clone = subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", f"https://github.com/{name}.git", str(destination)], capture_output=True, text=True, timeout=240)
    if clone.returncode: temporary.cleanup(); raise RuntimeError(clone.stderr.strip() or "git clone failed")
    globs = sorted({item for pattern in patterns for suffix in SUFFIXES[pattern] for item in (f"*{suffix}", f"**/*{suffix}")})
    setup = subprocess.run(["git", "-C", str(destination), "sparse-checkout", "set", "--no-cone", *globs], capture_output=True, text=True, timeout=180)
    checkout = subprocess.run(["git", "-C", str(destination), "checkout"], capture_output=True, text=True, timeout=180)
    if setup.returncode or checkout.returncode: temporary.cleanup(); raise RuntimeError((setup.stderr or checkout.stderr).strip() or "sparse checkout failed")
    return destination, temporary


def record(row: Candidate, repo: str, licence: str, commit: str, path: Path) -> dict[str, Any]:
    claim: dict[str, Any] = {"type": row.kind, "column": row.column}
    if row.kind == "accepted_values": claim["values"] = row.values
    else: claim["expr"] = row.check
    return {"class": "positive", "input": {"sentence": row.message, "column_name": row.column, "table_name": str(path), "schema_context": row.context}, "target": {"claim": claim}, "source_kind": "github_clone", "source_document": str(path), "source": {"repository": repo, "path": str(path), "commit": commit, "licence": licence, "pattern": row.pattern, "line": row.line, "adjacent_check": row.check}}


def harvest_repo(name: str, entry: dict[str, Any], progress: dict[str, Any]) -> None:
    if shutil.disk_usage(tempfile.gettempdir()).free < MIN_FREE_BYTES: raise StopForSpace("free space below 5 GB; stopping before clone")
    patterns = [pattern for pattern in entry["patterns"] if pattern not in entry.get("completed_patterns", [])]
    if not patterns: entry["status"] = "complete"; return
    first_clone = not entry.get("batches")
    try: worktree, temporary = clone_repo(name, patterns)
    except Exception as error:
        entry.update({"status": "clone_failed", "error": str(error)[:1000]}); progress["totals"]["repos_clone_failed"] += 1; atomic_json(PROGRESS, progress); refresh(progress); return
    try:
        commit = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
        stats_by_pattern = {pattern: blank_stats() for pattern in patterns}; rows: list[dict[str, Any]] = []; seen: set[str] = set()
        suffixes = {suffix for pattern in patterns for suffix in SUFFIXES[pattern]}
        for file in worktree.rglob("*"):
            if not file.is_file() or file.suffix.lower() not in suffixes or ".git" in file.parts or file.stat().st_size > 2_000_000: continue
            try: source = file.read_text(encoding="utf-8", errors="replace")
            except OSError: continue
            relative = file.relative_to(worktree)
            for pattern in patterns:
                if file.suffix.lower() not in SUFFIXES[pattern]: continue
                stats = stats_by_pattern[pattern]; stats["files_scanned"] += 1
                for item in candidates_for(pattern, source):
                    stats["candidates"] += 1
                    if item.message is None: stats["missing_message"] += 1; continue
                    if not accepts(item): stats["rejected"] += 1; continue
                    fingerprint = hashlib.sha256("\0".join((name, str(relative), item.pattern, item.message, item.check)).encode()).hexdigest()
                    if fingerprint in seen: continue
                    seen.add(fingerprint); rows.append(record(item, name, entry["licence"], commit, relative)); stats["pairs_extracted"] += 1
        batch = RAW / f"repo-{hashlib.sha256(name.encode()).hexdigest()[:16]}-{len(entry.get('completed_patterns', [])) + 1:02d}"
        write_jsonl(batch.with_suffix(".jsonl"), rows)
        atomic_json(batch.with_suffix(".manifest.json"), {"repository": name, "licence": entry["licence"], "commit": commit, "patterns": patterns, "stats_by_pattern": stats_by_pattern, "rows": len(rows), "completed_at": now()})
        entry["completed_patterns"] = sorted(set(entry.get("completed_patterns", [])) | set(patterns)); entry.setdefault("batches", []).append(batch.name); entry.update({"status": "complete", "commit": commit, "completed_at": now()})
        if first_clone: progress["totals"]["repos_cloned"] += 1
        for pattern, stats in stats_by_pattern.items(): add_stats(progress["totals"]["by_pattern"].setdefault(pattern, blank_stats()), stats); add_stats(progress["totals"], stats)
        atomic_json(PROGRESS, progress); refresh(progress); print(json.dumps({"repository": name, "pairs_extracted": len(rows), "patterns": patterns}, sort_keys=True), flush=True)
    finally: temporary.cleanup()


def harvest_ready(progress: dict[str, Any], patterns: set[str], limit: int | None = None) -> None:
    harvested = 0
    for name, entry in progress["repos"].items():
        if entry["status"] == "pending" and set(entry["patterns"]).intersection(patterns): harvest_repo(name, entry, progress)
        if entry["status"] == "complete" and set(entry.get("completed_patterns", [])).intersection(patterns):
            harvested += 1
            if limit is not None and harvested >= limit:
                break


def self_test() -> int:
    fixture = '''\nif status not in {"pending", "shipped"}:\n    raise ValueError("Status must be one of: pending, shipped")\nassert len(code) == 3, "currency code must be exactly 3 characters"\n'''
    rows = python_candidates(fixture)
    assert len(rows) == 2 and all(accepts(row) for row in rows), rows
    assert rows[0].values == ["pending", "shipped"] and rows[1].kind == "expression", rows
    codebooks = codebook_candidates("GENDER 1 = Male; 2 = Female; 9 = Unknown")
    assert codebooks and accepts(codebooks[0]), "codebook extraction failed"
    ts_fixture = '''const schema = z.string().refine(value => value.startsWith("x"), "Value must start with x"), next: z.string().min(1, "Required");'''
    ts_rows = ts_candidates(ts_fixture)
    assert len(ts_rows) == 2 and ts_rows[0].message == "Value must start with x", ts_rows
    joi_rows = ts_candidates('Joi.string().valid("a", "b").messages({"any.only": "Code must be one of: a, b"});')
    assert joi_rows[0].values == ["a", "b"], joi_rows
    print("self-test passed")
    return 0


def clear_restart_output() -> None:
    """Delete only this miner's generated batches before a deliberate restart."""
    for path in RAW.glob("repo-*.jsonl"):
        path.unlink()
    for path in RAW.glob("repo-*.manifest.json"):
        path.unlink()
    for path in (PROGRESS, REPOS, RAW / "report.json"):
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--resume", action="store_true"); parser.add_argument("--restart", action="store_true", help="replace this miner's empty/incomplete v6 checkpoint"); parser.add_argument("--pages-per-partition", type=int, default=1, choices=range(1, 11)); parser.add_argument("--max-repos-per-pattern", type=int, default=5, help="per discovery round; prevents one common pattern starving others"); parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test: return self_test()
    RAW.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try: owner = int(LOCK.read_text(encoding="ascii").strip()); os.kill(owner, 0)
        except (ValueError, ProcessLookupError): LOCK.unlink()
        else: raise SystemExit(f"another miner owns {LOCK} (pid {owner})")
    LOCK.write_text(str(os.getpid()), encoding="ascii")
    try:
        if args.restart:
            clear_restart_output()
            progress = initial_progress(); atomic_json(PROGRESS, progress)
        elif PROGRESS.exists():
            if not args.resume: raise SystemExit(f"{PROGRESS} exists; use --resume")
            progress = json.loads(PROGRESS.read_text(encoding="utf-8"))
            if progress.get("schema_version") != SCHEMA_VERSION: raise SystemExit("incompatible checkpoint schema")
        else: progress = initial_progress(); atomic_json(PROGRESS, progress)
        refresh(progress); last_search = 0.0
        while True:
            made_progress = False
            # One page and a bounded clone batch per family, then rotate.  The
            # rate table becomes useful early instead of after the largest
            # source family has exhausted all ten discovery pages.
            for part in progress["partitions"]:
                if part["status"] == "complete":
                    continue
                made_progress = True
                last_search = discover(part, progress, args.pages_per_partition, last_search)
                harvest_ready(progress, {part["pattern"]}, args.max_repos_per_pattern)
            if not made_progress:
                break
        return 0
    except StopForSpace as error:
        print(error, file=sys.stderr, flush=True); return 2
    finally: LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
