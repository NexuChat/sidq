#!/usr/bin/env python3
"""Detached worker for the Python ``must be one of`` error-message seam.

This is intentionally only an orchestration shim.  AST extraction, local
clone harvesting, sentence capping, checkpoint writes, and disk protection
all remain in :mod:`mine_code_corpora`.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
from dataclasses import replace
from pathlib import Path

import mine_code_corpora as miner

RAW = Path("data/claims/raw-p1")
PATTERN = "py_must_be_one_of"
QUERIES = (
    '"must be one of" ValidationError language:Python NOT is:archived',
    '"must be one of" ValueError language:Python NOT is:archived',
    '"must be one of" raise language:Python NOT is:archived',
)


def configure_miner() -> None:
    """Point the existing checkpointing implementation at this worker only."""
    miner.RAW = RAW
    miner.REPOS = RAW / "repos-to-clone.jsonl"
    miner.PROGRESS = RAW / "_progress.json"
    miner.LOCK = RAW / ".mine.lock"
    original_record = miner.make_record
    original_candidates = miner.candidates_for

    def record(*args, **kwargs):  # type: ignore[no-untyped-def]
        row = original_record(*args, **kwargs)
        row["source_kind"] = "error_msg_py_oneof"
        row["source"]["repo"] = row["source"]["repository"]
        return row

    def sentence_values(sentence: str) -> list[str | int | float]:
        """Return only the option tokens explicitly named after the phrase."""
        match = re.search(r"\bmust be one of\b\s*:?[\s]*(.*)", sentence, re.IGNORECASE)
        if not match:
            return []
        tail = match.group(1).strip().rstrip(".")
        quoted = re.findall(r"['\"`]([^'\"`]+)['\"`]", tail)
        tokens = quoted or [
            token.strip(" [](){}\t")
            for token in re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", tail)
        ]
        values: list[str | int | float] = []
        for token in tokens:
            token = token.strip().rstrip(".")
            if not token:
                continue
            if re.fullmatch(r"-?\d+", token):
                values.append(int(token))
            elif re.fullmatch(r"-?\d+\.\d+", token):
                values.append(float(token))
            else:
                values.append(token)
        return list(dict.fromkeys(values))

    def candidates(pattern: str, source: str):  # type: ignore[no-untyped-def]
        rows = original_candidates(pattern, source)
        return [
            replace(row, evidence=values)
            for row in rows
            if (values := sentence_values(row.description or ""))
            if row.kind == "accepted_values"
            and row.description
            and "must be one of" in row.description.casefold()
            and re.search(r"\braise\s+(?:ValidationError|ValueError)\s*\(", row.context)
        ]

    miner.make_record = record
    miner.candidates_for = candidates


def persist(progress: dict) -> None:
    miner.atomic_json(miner.PROGRESS, progress)
    miner.refresh_repos(progress)
    miner.refresh_report(progress)


def initial_progress() -> dict:
    progress = miner.initial_progress()
    progress["partitions"] = [
        {"id": f"p1-{index}", "pattern": PATTERN, "query": query,
         "status": "pending", "page": 1}
        for index, query in enumerate(QUERIES, 1)
    ]
    progress["worker"] = {
        "phrase": "must be one of",
        "language": "Python",
        "source_kind": "error_msg_py_oneof",
        "licence_check": "gh api repos/{owner}/{name}",
    }
    return progress


def licence_repo(name: str, progress: dict) -> None:
    """Use the requested REST endpoint once per repository before cloning."""
    entry = progress["repos"][name]
    try:
        metadata = miner.gh_json(f"repos/{name}")
    except Exception as error:
        entry.update({"status": "licence_failed", "error": str(error)[:1000]})
        persist(progress)
        return
    licence = ((metadata.get("license") or {}).get("spdx_id"))
    entry["licence"] = licence
    entry["licence_allowed"] = licence in miner.ALLOWED_LICENSES
    entry["branch"] = metadata.get("default_branch")
    if entry["licence_allowed"]:
        entry["status"] = "pending"
        progress["totals"]["repos_licenced"] += 1
    else:
        entry["status"] = "skipped_licence"
        progress["totals"]["repos_skipped_licence"] += 1
    persist(progress)


def discover(partition: dict, progress: dict, last_search: float) -> float:
    # GitHub code search does not support repository stars/size/pushed
    # qualifiers.  The three disjoint validation terms below are supported
    # code-search partitions; licence checks and clone harvesting are local.
    elapsed = time.monotonic() - last_search
    if elapsed < miner.SEARCH_INTERVAL_SECONDS:
        time.sleep(miner.SEARCH_INTERVAL_SECONDS - elapsed)
    endpoint = "search/code?" + urllib.parse.urlencode(
        {"q": partition["query"], "per_page": 100, "page": partition["page"]}
    )
    payload = miner.gh_json(endpoint)
    partition["search_total_count"] = payload.get("total_count", 0)
    new_names: list[str] = []
    for item in payload.get("items", []):
        name = item["repository"]["full_name"]
        if name in progress["repos"]:
            continue
        progress["repos"][name] = {
            "patterns": [PATTERN], "completed_patterns": [],
            "partitions": [partition["id"]], "status": "awaiting_licence",
        }
        progress["totals"]["repos_discovered"] += 1
        new_names.append(name)
    partition["page"] += 1
    partition["status"] = "complete"
    persist(progress)
    print(json.dumps({"discovery_partition": partition["id"], "new_repos": len(new_names)}, sort_keys=True), flush=True)
    for name in new_names:
        licence_repo(name, progress)
    return time.monotonic()


def main() -> int:
    configure_miner()
    RAW.mkdir(parents=True, exist_ok=True)
    if miner.LOCK.exists():
        try:
            owner = int(miner.LOCK.read_text(encoding="ascii").strip())
            __import__("os").kill(owner, 0)
        except (ValueError, ProcessLookupError):
            miner.LOCK.unlink()
        else:
            print(f"another p1 miner owns {miner.LOCK} (pid {owner})", file=sys.stderr)
            return 1
    miner.LOCK.write_text(str(__import__("os").getpid()), encoding="ascii")
    try:
        progress = json.loads(miner.PROGRESS.read_text(encoding="utf-8")) if miner.PROGRESS.exists() else initial_progress()
        miner.verify_progress(progress)
        persist(progress)
        last_search = 0.0
        for partition in progress["partitions"]:
            if partition["status"] != "complete":
                last_search = discover(partition, progress, last_search)
            miner.harvest_ready(progress, {PATTERN}, max_repos=3000)
        miner.harvest_ready(progress, {PATTERN}, max_repos=3000)
        return 0
    except miner.StopForSpace as error:
        print(str(error), file=sys.stderr, flush=True)
        return 2
    finally:
        miner.LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
