"""Label generated mutations with the fixture-backed Sidq engine."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import shutil
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from sidq.bot.action import run_engine
from sidq.models import Verdict
from sidq.resolver import Resolver

CONTEXT_KEYS = {
    "changed_files",
    "added_fields",
    "removed_fields",
    "referenced_fields",
    "touched_urns",
    "downstream_count",
}

# These pure transformations are correctly skipped: the bundled demo has neither
# a WHERE predicate nor a CTE to transform.  They remain part of the catalogue.
NOT_APPLICABLE_IN_DEMO = {
    "delete_where_filter": "no demo model has a WHERE filter to delete",
    "rename_cte": "no demo model contains a CTE to rename",
}


class LabelRecord(TypedDict):
    id: str
    family: str
    intent: str
    verdict: str
    reason_code: str | None
    rule_ids: list[str]
    evidence_kinds: list[str]
    context: dict[str, Any]
    engine_error: str | None


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Minimal test seam; production calls return the engine's Verdict."""

    decision: str
    reason_code: str | None
    findings: tuple[Any, ...]
    touched: tuple[Any, ...]


Engine = Callable[..., Verdict | EngineResult]


def _context(model_path: str, root: Path) -> dict[str, Any]:
    resolved = Resolver(root).resolve([model_path])
    assets = resolved.touched_assets
    return {
        "changed_files": [model_path],
        "added_fields": sorted({field for asset in assets for field in asset.added_fields}),
        "removed_fields": sorted({field for asset in assets for field in asset.removed_fields}),
        "referenced_fields": sorted(
            {
                f"{reference.dataset_urn}#{reference.field_path}"
                for asset in assets
                for reference in asset.referenced_fields
            }
        ),
        "touched_urns": sorted({asset.urn for asset in assets}),
        "downstream_count": 0,
    }


def _engine_result(verdict: Verdict | EngineResult, context: dict[str, Any]) -> LabelRecord:
    findings = tuple(verdict.findings)
    context["downstream_count"] = max(
        (
            int(evidence.detail.get("downstream_count", 0))
            for finding in findings
            for evidence in finding.evidence
            if isinstance(getattr(evidence, "detail", None), dict)
            and isinstance(evidence.detail.get("downstream_count", 0), int)
        ),
        default=0,
    )
    return {
        "verdict": verdict.decision,
        "reason_code": verdict.reason_code,
        "rule_ids": sorted({finding.rule_id for finding in findings}),
        "evidence_kinds": sorted(
            {evidence.kind for finding in findings for evidence in finding.evidence}
        ),
        "context": context,
        "engine_error": None,
    }


def label_record(
    record: dict[str, Any],
    *,
    demo_root: Path,
    fixture_dir: Path,
    engine: Engine = run_engine,
    scratch_root: Path | None = None,
) -> LabelRecord:
    """Copy one demo, replace one model, and label only from the engine result."""
    temporary = tempfile.TemporaryDirectory(dir=scratch_root)
    root = Path(temporary.name) / "dbt"
    try:
        shutil.copytree(demo_root, root)
        model_path = str(record["model_path"])
        target = root / model_path
        target.write_text(str(record["mutated_sql"]), encoding="utf-8", newline="\n")
        context = _context(model_path, root)
        try:
            result = engine(
                files=[model_path],
                repo_root=root,
                commit_sha=f"mutation:{record['id']}",
                mode="fixture",
                fixture_dir=fixture_dir,
            )
            labelled = _engine_result(result, context)
        except Exception as error:
            labelled = {
                "verdict": "error",
                "reason_code": None,
                "rule_ids": [],
                "evidence_kinds": [],
                "context": context,
                "engine_error": f"{type(error).__name__}: {str(error).replace(str(root.parent), '<temp>')}",
            }
        return {
            "id": str(record["id"]),
            "family": str(record["family"]),
            "intent": str(record["intent"]),
            **labelled,
        }
    finally:
        temporary.cleanup()


def _worker(arguments: tuple[dict[str, Any], str, str, str | None]) -> LabelRecord:
    record, demo_root, fixture_dir, scratch_root = arguments
    return label_record(
        record,
        demo_root=Path(demo_root),
        fixture_dir=Path(fixture_dir),
        scratch_root=Path(scratch_root) if scratch_root else None,
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSONL at {path}:{number}") from error
        if not isinstance(parsed, dict) or not isinstance(parsed.get("id"), str):
            raise TypeError(f"JSONL record at {path}:{number} has no string id")
        records.append(parsed)
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for record in sorted(records, key=lambda item: str(item["id"])):
            output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _table(rows: list[list[str]]) -> str:
    return "\n".join(
        ["| " + " | ".join(rows[0]) + " |", "|" + "|".join("---" for _ in rows[0]) + "|"]
        + ["| " + " | ".join(row) + " |" for row in rows[1:]]
    )


def build_report(records: Iterable[dict[str, Any]]) -> str:
    """Render the intentionally candid benchmark report from labelled JSONL."""
    ordered = sorted(records, key=lambda item: str(item["id"]))
    verdicts = ("BLOCK", "WARN", "PASS", "error")
    intents = ("harmful", "benign", "unknown")
    counts: dict[str, Counter[str]] = {intent: Counter() for intent in intents}
    for record in ordered:
        counts.setdefault(str(record.get("intent", "unknown")), Counter())[str(record.get("verdict"))] += 1
    confusion = [["generator intent", *verdicts]] + [
        [intent, *(str(counts[intent][verdict]) for verdict in verdicts)] for intent in intents
    ]
    misses = [item for item in ordered if item.get("intent") == "harmful" and item.get("verdict") == "PASS"]
    false_alarms = [item for item in ordered if item.get("intent") == "benign" and item.get("verdict") == "BLOCK"]
    family_counts: dict[str, Counter[str]] = defaultdict(Counter)
    family_totals: Counter[str] = Counter()
    for record in ordered:
        family_counts[str(record["family"])][str(record["verdict"])] += 1
        family_totals[str(record["family"])] += 1
    families = [["family", *verdicts]] + [
        [family, *(str(family_counts[family][verdict]) for verdict in verdicts)]
        for family in sorted(family_counts)
    ]
    lines = [
        "# Mutation Benchmark",
        "",
        f"Labelled mutations: {len(ordered)}. Labels are the fixture engine's verdicts; generator intent is retained only for comparison.",
        "",
        "## Confusion table",
        "",
        _table(confusion),
        "",
        *_coverage_section(ordered),
        "## Misses",
        "",
        "These are harmful-intent mutations that the engine passed. This is the published miss count.",
        "",
        f"Count: {len(misses)}",
        "",
    ]
    lines.extend(_family_rate_table("miss", misses, family_totals))
    lines.extend(_diff_samples(misses, DIFF_SAMPLE_LIMIT))
    lines.extend(["## False alarms", "", f"Count: {len(false_alarms)}", ""])
    lines.extend(_family_rate_table("false alarm", false_alarms, family_totals))
    lines.extend(_diff_samples(false_alarms, DIFF_SAMPLE_LIMIT))
    lines.extend(
        [
            "## Per-family breakdown",
            "",
            _table(families),
            "",
            "Not applicable to this demo corpus:",
            "",
            *[
                f"- `{family}` — {reason}."
                for family, reason in NOT_APPLICABLE_IN_DEMO.items()
            ],
            "",
            "## Limitations",
            "",
            "The generator is our own, so it tests the gate against our imagination, not against the real world. It is useful for reproducible regression measurement, but it cannot establish real-world coverage.",
            "",
        ]
    )
    return "\n".join(lines)


# A benchmark nobody can open is not evidence. Every diff was published before,
# which produced a 13 MB, 396,000-line document; the rates below carry the signal
# and the sample carries the texture. The count is always stated in full.
DIFF_SAMPLE_LIMIT = 12


def _coverage_section(records: list[dict[str, Any]]) -> list[str]:
    """State what the corpus could exercise, so a rate is not read as blindness.

    A miss rate is only a measure of the gate if the gate could have fired. A rule
    that needs downstream lineage cannot fire on a model whose radius is empty, and
    reporting that as a miss would blame the engine for a gap in the fixtures.
    """
    total = len(records)
    if not total:
        return []
    no_radius = sum(
        1 for item in records if not (item.get("context", {}).get("downstream_count") or 0)
    )
    fired: Counter[str] = Counter()
    for item in records:
        fired.update(str(rule) for rule in (item.get("rule_ids") or ()))
    rows = [["rule", "times fired"]]
    rows.extend(
        [f"`{rule}`", str(count)] for rule, count in fired.most_common()
    )
    return [
        "## What this corpus could exercise",
        "",
        "The generator's `intent` is not ground truth; it is a hypothesis about a",
        "mutation, and the label is the engine's verdict. A rate below is a measure",
        "of the gate only where the gate could have fired at all.",
        "",
        f"- **{no_radius:,} of {total:,} rows ({no_radius / total:.0%}) have no",
        "  downstream lineage in the fixtures.** Every rule that needs a blast",
        "  radius — `pii_exposure`, `critical_downstream`, `wide_blast_radius` — is",
        "  unreachable on those rows by construction, not by omission.",
        "- PII tags exist in these fixtures only on the legacy showcase model, so the",
        "  `expose_pii_tagged_column` family is largely a test of resolution rather",
        "  than of PII detection. Its rate belongs in that light.",
        "",
        "Rules that actually fired:",
        "",
        _table(rows),
        "",
    ]


def _family_rate_table(
    label: str, records: list[dict[str, Any]], totals: Counter[str]
) -> list[str]:
    """Rates per family: where the gate is blind, rather than one aggregate number."""
    if not records:
        return []
    by_family = Counter(str(item["family"]) for item in records)
    rows = [["family", f"{label}es", "generated", "rate"]]
    for family in sorted(by_family, key=lambda name: -by_family[name]):
        total = totals[family]
        share = f"{by_family[family] / total:.0%}" if total else "—"
        rows.append([f"`{family}`", str(by_family[family]), str(total), share])
    return [_table(rows), ""]


def _diff_samples(records: list[dict[str, Any]], limit: int) -> list[str]:
    """Publish a bounded, deterministic sample and say plainly what was withheld."""
    if not records:
        return ["None.", ""]
    sample = _limited_sample(records, limit)
    lines: list[str] = []
    if len(sample) < len(records):
        lines.extend(
            [
                (
                    f"Showing {len(sample)} of {len(records)} diffs, sampled "
                    "round-robin across families so every family that appears above "
                    "is represented. The full set is in "
                    "`data/benchmark/labelled.jsonl`."
                ),
                "",
            ]
        )
    for item in sample:
        lines.extend(
            [
                f"### {item['id']} ({item['family']})",
                "",
                "```diff",
                str(item.get("diff", "diff unavailable")),
                "```",
                "",
            ]
        )
    return lines


def _limited_sample(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Take a deterministic round-robin sample so a small smoke run has all families."""
    by_family: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in sorted(records, key=lambda item: str(item["id"])):
        by_family[(str(record["family"]), str(record["model_path"]))].append(record)
    sample: list[dict[str, Any]] = []
    index = 0
    families = sorted(by_family)
    while len(sample) < limit:
        added = False
        for family in families:
            if index < len(by_family[family]):
                sample.append(by_family[family][index])
                added = True
                if len(sample) == limit:
                    break
        if not added:
            break
        index += 1
    return sample


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--demo-root", type=Path, default=Path("demo/dbt"))
    parser.add_argument("--fixture-dir", type=Path, default=Path("tests/fixtures/graph"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--report", nargs="?", const=Path("docs/BENCHMARK.md"), type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    source = read_jsonl(args.input)
    if args.limit is not None:
        source = _limited_sample(source, args.limit)
    existing = read_jsonl(args.out) if args.resume else []
    retained = {str(item["id"]): item for item in existing}
    pending = [item for item in source if str(item["id"]) not in retained]
    arguments = [
        (item, str(args.demo_root.resolve()), str(args.fixture_dir.resolve()), str(args.scratch_root.resolve()) if args.scratch_root else None)
        for item in pending
    ]
    if args.jobs == 1:
        labelled = [_worker(item) for item in arguments]
    else:
        with multiprocessing.Pool(args.jobs) as pool:
            labelled = pool.map(_worker, arguments)
    retained.update({str(item["id"]): item for item in labelled})
    completed = [retained[str(item["id"])] for item in source if str(item["id"]) in retained]
    write_jsonl(args.out, completed)
    if args.report is not None:
        input_by_id = {str(item["id"]): item for item in source}
        report_records = [{**input_by_id[str(item["id"])], **item} for item in completed]
        args.report.write_text(build_report(report_records), encoding="utf-8", newline="\n")
    print(f"labelled {len(labelled)} mutations; wrote {len(completed)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
