#!/usr/bin/env python3
"""Mine licence-audited documentation-to-dbt-test examples from public repos.

The script deliberately uses GitHub's REST API only for repository metadata and
commit resolution, then downloads a source archive at that exact commit.  It
does not clone repositories (and therefore never depends on a local git state).
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
ALLOWED_LICENSES = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "0BSD",
    "Unlicense",
    "CC0-1.0",
}
# These packages are public dbt projects selected before collection.  Every
# entry is still checked against current GitHub licence metadata before use.
REPOSITORIES = (
    "dbt-labs/jaffle_shop",
    "dbt-labs/dbt-utils",
    "dbt-labs/dbt-expectations",
    "dbt-labs/dbt-audit-helper",
    "dbt-labs/dbt-codegen",
    "dbt-labs/dbt-project-evaluator",
    "calogica/dbt-date",
    "calogica/dbt-snowflake-monitoring",
    "fivetran/dbt_fivetran_utils",
    "fivetran/dbt_fivetran_log",
    "fivetran/dbt_shopify",
    "fivetran/dbt_stripe",
    "fivetran/dbt_google_ads",
    "fivetran/dbt_facebook_ads",
    "fivetran/dbt_hubspot",
    "fivetran/dbt_zendesk",
    "fivetran/dbt_salesforce",
    "fivetran/dbt_linkedin_ads",
    "fivetran/dbt_twitter_ads",
    "fivetran/dbt_marketo",
    "fivetran/dbt_pinterest",
    "fivetran/dbt_tiktok_ads",
    "fivetran/dbt_amplitude",
    "fivetran/dbt_apple_store",
    "fivetran/dbt_klaviyo",
    "fivetran/dbt_intercom",
    "fivetran/dbt_github",
    "fivetran/dbt_netsuite",
    "fivetran/dbt_greenhouse",
    "fivetran/dbt_drift",
    "fivetran/dbt_quickbooks",
    "fivetran/dbt_qualtrics",
    "fivetran/dbt_lever",
    "fivetran/dbt_workday",
    "fivetran/dbt_recharge",
    "fivetran/dbt_mailchimp",
    "fivetran/dbt_pardot",
    "fivetran/dbt_twilio",
    "fivetran/dbt_asana",
    "fivetran/dbt_sap",
    "fivetran/dbt_recurly",
    "fivetran/dbt_zuora",
    "fivetran/dbt_microsoft_ads",
    "snowplow/dbt-snowplow-web",
    "snowplow/dbt-snowplow-mobile",
    "snowplow/dbt-snowplow-media-player",
    "data-mie/dbt_artifacts",
)
HARD_NEGATIVE_RE = re.compile(
    r"\b(source of truth|updated (regularly|daily|weekly|monthly|hourly)|"
    r"important (for|to)|used (for|by)|powers?|supports?|enables?|"
    r"contains? (information|data)|tracks?|represents?|stores?|"
    r"provides?|central|canonical|primary|key business)\b",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9`])")


@dataclass(frozen=True)
class RepoInfo:
    name: str
    commit: str
    licence: str
    default_branch: str


def request_json(url: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sidq-dbt-claim-miner/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "sidq-dbt-claim-miner/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_repo_info(repo: str) -> RepoInfo | None:
    try:
        metadata = request_json(f"https://api.github.com/repos/{repo}")
        licence = (metadata.get("license") or {}).get("spdx_id")
        if licence not in ALLOWED_LICENSES:
            print(f"skip {repo}: licence={licence or 'missing/ambiguous'}", file=sys.stderr)
            return None
        branch = metadata["default_branch"]
        ref = request_json(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(branch)}")
        return RepoInfo(repo, ref["sha"], licence, branch)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
        # GitHub's public API is capped at 60 requests/hour without a token.
        # The repository page presents the same SPDX badge and the commit page
        # resolves HEAD to a full SHA, so this fallback remains auditable.
        page = request_text(f"https://github.com/{repo}")
        licence_match = re.search(r"\b(Apache-2\.0|MIT|BSD-2-Clause|BSD-3-Clause|0BSD|Unlicense|CC0-1\.0) license\b", page)
        licence = licence_match.group(1) if licence_match else None
        if licence not in ALLOWED_LICENSES:
            print(f"skip {repo}: licence={licence or 'missing/ambiguous'}", file=sys.stderr)
            return None
        commit_page = request_text(f"https://github.com/{repo}/commit/HEAD")
        sha_match = re.search(r"/commit/([0-9a-f]{40})", commit_page)
        if not sha_match:
            raise KeyError("could not resolve exact HEAD commit from GitHub page")
        return RepoInfo(repo, sha_match.group(1), licence, "HEAD")


def fetch_archive(info: RepoInfo) -> zipfile.ZipFile:
    url = f"https://codeload.github.com/{info.name}/zip/{info.commit}"
    request = urllib.request.Request(url, headers={"User-Agent": "sidq-dbt-claim-miner/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return zipfile.ZipFile(io.BytesIO(response.read()))


def schema_paths(archive: zipfile.ZipFile) -> Iterable[str]:
    for name in archive.namelist():
        lower = name.lower()
        basename = lower.rsplit("/", 1)[-1]
        if not basename.endswith((".yml", ".yaml")):
            continue
        if basename in {"packages.yml", "dependencies.yml", "dbt_project.yml"}:
            continue
        # dbt packages can include generated target data; it is not source docs.
        if "/target/" in lower or "/dbt_packages/" in lower:
            continue
        yield name


def text_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) < 8 or value.startswith("{{"):
        return None
    return value


def sentences(description: str) -> list[str]:
    values = [s.strip(" -\t\n") for s in SENTENCE_RE.split(description) if len(s.strip()) >= 8]
    return values or [description]


def test_items(tests: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    if not isinstance(tests, list):
        return
    for item in tests:
        if isinstance(item, str):
            yield item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, config = next(iter(item.items()))
            if isinstance(name, str):
                yield name, config if isinstance(config, dict) else {}


def node_tests(node: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """dbt 1.8 renamed `tests` to `data_tests`; support both schema forms."""
    yield from test_items(node.get("tests"))
    yield from test_items(node.get("data_tests"))


def clean_values(value: Any) -> list[Any] | None:
    if isinstance(value, list) and value and all(isinstance(v, (str, int, float, bool)) for v in value):
        return value
    return None


def map_test(name: str, config: dict[str, Any], column: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return executable predicate, or a reason the test is intentionally dropped."""
    short = name.rsplit(".", 1)[-1]
    args = config.get("arguments", config)
    if not isinstance(args, dict):
        args = {}
    if short in {"unique", "not_null"}:
        if not column:
            return None, "model_level_unique_or_not_null"
        return {"type": short, "column": column}, None
    if short == "accepted_values":
        values = clean_values(args.get("values"))
        if column and values is not None:
            return {"type": "accepted_values", "column": column, "values": values}, None
        return None, "accepted_values_missing_literal_values"
    if short == "relationships":
        to, field = args.get("to"), args.get("field")
        if column and isinstance(to, str) and isinstance(field, str):
            return {"type": "relationships", "column": column, "expr": f"to={to};field={field}"}, None
        return None, "relationships_missing_to_or_field"
    if short in {"expression_is_true", "expect_column_values_to_match_regex", "expect_column_values_to_not_match_regex"}:
        expression = args.get("expression") or args.get("regex")
        if column and isinstance(expression, str) and expression.strip():
            return {"type": "expression", "column": column, "expr": expression.strip()}, None
        return None, "expression_missing_literal"
    return None, f"unmapped_test:{name}"


def context_for(model: dict[str, Any], column: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if isinstance(model.get("meta"), dict):
        materialized = model["meta"].get("materialized")
        if isinstance(materialized, str):
            parts.append(f"materialized={materialized}")
    if column and isinstance(column.get("data_type"), str):
        parts.append(f"data_type={column['data_type']}")
    return "; ".join(parts) or "dbt schema.yml"


def base_record(sentence: str, table: str, column: str | None, context: str, info: RepoInfo, path: str) -> dict[str, Any]:
    return {
        "input": {"sentence": sentence, "column_name": column, "table_name": table, "schema_context": context},
        "source": {"repo": info.name, "path": path, "commit": info.commit, "licence": info.licence},
    }


def mine_schema(doc: Any, info: RepoInfo, path: str, dropped: collections.Counter[str]) -> list[dict[str, Any]]:
    if not isinstance(doc, dict):
        return []
    records: list[dict[str, Any]] = []
    for section in ("models", "sources", "seeds", "snapshots"):
        nodes = doc.get(section)
        if not isinstance(nodes, list):
            continue
        for model in nodes:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                continue
            table = model["name"]
            model_desc = text_value(model.get("description"))
            model_context = context_for(model, None)
            mapped_model: list[dict[str, Any]] = []
            for test_name, config in node_tests(model):
                predicate, reason = map_test(test_name, config, None)
                if predicate:
                    mapped_model.append(predicate)
                else:
                    dropped[reason or "unknown"] += 1
            if model_desc:
                for sentence in sentences(model_desc):
                    base = base_record(sentence, table, None, model_context, info, path)
                    if mapped_model:
                        for predicate in mapped_model:
                            records.append(base | {"target": {"claim": predicate}, "class": "positive"})
                    else:
                        label = "hard_negative" if HARD_NEGATIVE_RE.search(sentence) else "negative"
                        records.append(base | {"target": {"claim": None}, "class": label})
            columns = model.get("columns")
            if not isinstance(columns, list):
                continue
            for column in columns:
                if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                    continue
                description = text_value(column.get("description"))
                if not description:
                    continue
                name = column["name"]
                mapped: list[dict[str, Any]] = []
                for test_name, config in node_tests(column):
                    predicate, reason = map_test(test_name, config, name)
                    if predicate:
                        mapped.append(predicate)
                    else:
                        dropped[reason or "unknown"] += 1
                for sentence in sentences(description):
                    base = base_record(sentence, table, name, context_for(model, column), info, path)
                    if mapped:
                        for predicate in mapped:
                            records.append(base | {"target": {"claim": predicate}, "class": "positive"})
                    else:
                        label = "hard_negative" if HARD_NEGATIVE_RE.search(sentence) else "negative"
                        records.append(base | {"target": {"claim": None}, "class": label})
    return records


def mine_repo(info: RepoInfo, dropped: collections.Counter[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    archive = fetch_archive(info)
    root = archive.namelist()[0].split("/", 1)[0] + "/"
    for archive_path in schema_paths(archive):
        try:
            document = yaml.safe_load(archive.read(archive_path))
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as error:
            print(f"skip unreadable {info.name}:{archive_path}: {error}", file=sys.stderr)
            continue
        records.extend(mine_schema(document, info, archive_path.removeprefix(root), dropped))
    return records


def stable_key(record: dict[str, Any]) -> str:
    return json.dumps(record["input"], sort_keys=True) + json.dumps(record["target"], sort_keys=True)


def choose_records(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Deduplicate then enforce 50/35/15 composition without synthetic examples."""
    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        key = (record["source"]["repo"], stable_key(record))
        if key not in seen:
            seen.add(key)
            grouped[record["class"]].append(record)
    desired = {"positive": limit * 50 // 100, "negative": limit * 35 // 100, "hard_negative": limit * 15 // 100}
    selected: list[dict[str, Any]] = []
    for label, count in desired.items():
        items = sorted(grouped[label], key=lambda r: hashlib.sha256(stable_key(r).encode()).hexdigest())
        selected.extend(items[:count])
    # If a rare natural class prevents the target size, use the remaining real
    # examples of the other classes; never manufacture rows to meet a quota.
    if len(selected) < limit:
        selected_keys = {stable_key(r) for r in selected}
        remainder = [r for items in grouped.values() for r in items if stable_key(r) not in selected_keys]
        remainder.sort(key=lambda r: hashlib.sha256(stable_key(r).encode()).hexdigest())
        selected.extend(remainder[: limit - len(selected)])
    return selected


def split_by_repository(records: list[dict[str, Any]], target_eval: int = 200) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in records:
        by_repo[record["source"]["repo"]].append(record)
    repos = sorted(by_repo, key=lambda r: hashlib.sha256(("eval:" + r).encode()).hexdigest())
    # Hold out several complete repositories.  We deliberately discard excess
    # rows from those repositories rather than returning them to train: a row
    # cap is not permission to mix a house style across splits.
    eval_repos = repos[: min(5, len(repos))]
    held_out = set(eval_repos)
    evaluation_candidates = [r for r in records if r["source"]["repo"] in held_out]
    evaluation_candidates.sort(key=lambda r: hashlib.sha256(stable_key(r).encode()).hexdigest())
    evaluation = evaluation_candidates[:target_eval]
    train = [r for r in records if r["source"]["repo"] not in held_out]
    return train, evaluation, eval_repos


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_datasheet(records: list[dict[str, Any]], train: list[dict[str, Any]], evaluation: list[dict[str, Any]], held_out: list[str], dropped: collections.Counter[str], skipped: list[str]) -> None:
    counts = collections.Counter(record["class"] for record in records)
    licences = collections.Counter(record["source"]["licence"] for record in records)
    repos = collections.Counter(record["source"]["repo"] for record in records)
    lines = [
        "# dbt documentation claim dataset",
        "",
        "## Collection",
        "",
        "`scripts/mine_dbt_claims.py` queries GitHub repository metadata (or, after the public API rate limit, GitHub's displayed SPDX licence badge), rejects every repository whose licence is not in the permissive allowlist (MIT, Apache-2.0, BSD-2/3-Clause, 0BSD, Unlicense, CC0-1.0), resolves the default-branch head SHA, then downloads the public source archive at that SHA. It parses source `*.yml`/`*.yaml` dbt schema files with PyYAML; generated `target/` and installed `dbt_packages/` files are excluded. Every row preserves repository, source path, exact commit and GitHub-reported SPDX licence.",
        "",
        "A positive is a description sentence adjacent to a column/model test that maps exactly to the engine vocabulary. A negative is a documented field with no mapped adjacent test. A hard negative is the same, selected using an explicit cue list for operational/business-sounding prose (for example `source of truth`, `updated daily`, `important for`, or `used by`). No text or predicate is generated by this collector.",
        "",
        "## Counts",
        "",
        f"- Total: {len(records)}",
        f"- Train: {len(train)}",
        f"- Eval: {len(evaluation)}",
        f"- Positive: {counts['positive']}",
        f"- Negative: {counts['negative']}",
        f"- Hard negative: {counts['hard_negative']}",
        "",
        "## Licence distribution",
        "",
    ]
    lines.extend(f"- {licence}: {count}" for licence, count in sorted(licences.items()))
    lines.extend(["", "## Repository distribution", ""])
    lines.extend(f"- {repo}: {count}" for repo, count in sorted(repos.items()))
    lines.extend(["", "## Held-out repositories", ""])
    lines.extend(f"- {repo}" for repo in held_out)
    lines.extend(["", "## Dropped tests", ""])
    if dropped:
        lines.extend(f"- {reason}: {count}" for reason, count in dropped.most_common())
    else:
        lines.append("- None")
    lines.extend(["", "## Skipped repositories", ""])
    lines.extend(f"- {repo}" for repo in skipped) if skipped else lines.append("- None")
    lines.extend([
        "",
        "## Known biases and limitations",
        "",
        "- dbt schema documentation and generic-test conventions overrepresent `unique` and `not_null`; these are not a representative sample of all data-quality claims.",
        "- Descriptions are in projects that publish dbt metadata and are biased toward English, analytics engineering teams, and their package conventions.",
        "- The hard-negative cue heuristic is deliberately conservative and lexical; it does not establish that every business/operational claim is uncheckable in every organisation.",
        "- A repository-level split prevents house-style leakage, but it does not eliminate similarity among related package ecosystems.",
        "- Licence provenance is GitHub's SPDX classification at collection time. Re-run the miner before redistribution if a repository's licence status changes.",
        "",
    ])
    (OUT / "DATASHEET.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3000, help="maximum dataset rows (default: 3000)")
    parser.add_argument("--repos", nargs="*", default=REPOSITORIES, help="owner/repo list; defaults to curated dbt projects")
    parser.add_argument("--pause", type=float, default=0.15, help="seconds between repositories")
    parser.add_argument("--collect", action="store_true", help="append raw candidates for a resumable collection")
    parser.add_argument("--reset-collection", action="store_true", help="clear a prior resumable collection before collecting")
    parser.add_argument("--finalize", action="store_true", help="write dataset files from a resumable collection")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    OUT.mkdir(parents=True, exist_ok=True)
    cache_path = OUT / ".candidates.jsonl"
    stats_path = OUT / ".collection_stats.json"
    if args.finalize:
        if not cache_path.exists() or not stats_path.exists():
            parser.error("no resumable collection found")
        raw = [json.loads(line) for line in cache_path.read_text(encoding="utf-8").splitlines()]
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        dropped = collections.Counter(stats.get("dropped", {}))
        skipped = list(stats.get("skipped", []))
        selected = choose_records(raw, args.limit)
        train, evaluation, held_out = split_by_repository(selected)
        write_jsonl(OUT / "train.jsonl", train)
        write_jsonl(OUT / "eval.jsonl", evaluation)
        write_datasheet(selected, train, evaluation, held_out, dropped, skipped)
        cache_path.unlink()
        stats_path.unlink()
        counts = collections.Counter(record["class"] for record in selected)
        print(json.dumps({"total": len(selected), "train": len(train), "eval": len(evaluation), "classes": counts, "licences": collections.Counter(r["source"]["licence"] for r in selected), "dropped": dropped}, default=dict, sort_keys=True))
        return 0
    if args.reset_collection:
        cache_path.unlink(missing_ok=True)
        stats_path.unlink(missing_ok=True)
    raw: list[dict[str, Any]] = []
    dropped: collections.Counter[str] = collections.Counter()
    skipped: list[str] = []
    for repo in args.repos:
        try:
            info = fetch_repo_info(repo)
            if info is None:
                skipped.append(repo)
            else:
                mined = mine_repo(info, dropped)
                print(f"mined {repo}: {len(mined)} candidate rows", file=sys.stderr)
                raw.extend(mined)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, zipfile.BadZipFile) as error:
            print(f"skip {repo}: {error}", file=sys.stderr)
            skipped.append(repo)
        time.sleep(args.pause)
    if args.collect:
        with cache_path.open("a", encoding="utf-8") as handle:
            for record in raw:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        prior = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {"dropped": {}, "skipped": []}
        prior_dropped = collections.Counter(prior.get("dropped", {}))
        prior_dropped.update(dropped)
        prior["dropped"] = dict(prior_dropped)
        prior["skipped"] = sorted(set(prior.get("skipped", [])) | set(skipped))
        stats_path.write_text(json.dumps(prior, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"collected": len(raw), "dropped": dropped, "skipped": skipped}, default=dict, sort_keys=True))
        return 0
    selected = choose_records(raw, args.limit)
    train, evaluation, held_out = split_by_repository(selected)
    write_jsonl(OUT / "train.jsonl", train)
    write_jsonl(OUT / "eval.jsonl", evaluation)
    write_datasheet(selected, train, evaluation, held_out, dropped, skipped)
    counts = collections.Counter(record["class"] for record in selected)
    print(json.dumps({"total": len(selected), "train": len(train), "eval": len(evaluation), "classes": counts, "licences": collections.Counter(r["source"]["licence"] for r in selected), "dropped": dropped}, default=dict, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
