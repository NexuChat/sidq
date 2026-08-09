"""Measure duplicated swarm work at the workers' own evidence boundary.

DataHub exposes only the latest receipt, not an append-only examination
history, so collisions cannot be counted from the catalog after the fact.
This module instead measures what each surviving worker reported about itself.
A worker reporting its own duplicated effort is reporting against itself rather
than flattering itself, but this is still a self-reported measurement, not an
independent verification.  The coverage ledger that follows in ``swarm-demo``
is still read independently out of DataHub.

Parsing and filesystem I/O are kept at the edges.  ``measure_overlap`` is a
pure reduction over validated ``WorkerAccount`` values.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sidq.agent.swarm import WorkerRun
from sidq.serialization import canonical_json

_SUMMARY_COUNTS = (
    "examined",
    "receipts_written",
    "findings",
    "vouched_by_peer",
    "vouched_unattributed",
    "blocked_by_receipt",
    "write_failures",
)


class SwarmReportError(ValueError):
    """A worker report cannot be treated as evidence."""


@dataclass(frozen=True, slots=True)
class WorkerAccount:
    """The overlap-relevant part of one validated worker report."""

    worker_id: str
    swarm_run: str
    examined: tuple[str, ...]
    written: tuple[str, ...]
    summary: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SwarmOverlap:
    """Duplicated examination counts over the worker reports that survived."""

    swarm_run: str
    expected_reports: int
    found_reports: int
    total_examinations: int
    distinct_assets_examined: int
    duplicated_examinations: int
    overlapping_assets: Mapping[str, tuple[str, ...]]

    def summary(self) -> dict[str, object]:
        return {
            "swarm_run": self.swarm_run,
            "expected_reports": self.expected_reports,
            "found_reports": self.found_reports,
            "total_examinations": self.total_examinations,
            "distinct_assets_examined": self.distinct_assets_examined,
            "duplicated_examinations": self.duplicated_examinations,
            "overlapping_assets": dict(self.overlapping_assets),
        }

    def render(self) -> list[str]:
        lines = [
            f"Swarm overlap — run {self.swarm_run or '(none reported)'}",
            "",
            (
                f"  worker reports found     {self.found_reports} "
                f"of {self.expected_reports} expected"
            ),
            f"  total examinations       {self.total_examinations}",
            f"  distinct assets examined {self.distinct_assets_examined}",
            f"  duplicated examinations  {self.duplicated_examinations}",
            "",
            "Assets examined by more than one worker:",
        ]
        if self.overlapping_assets:
            for urn, workers in sorted(self.overlapping_assets.items()):
                lines.append(f"  {urn}  ({', '.join(workers)})")
        else:
            lines.append("  (none)")
        lines.extend(
            [
                "",
                (
                    "Evidence boundary: this is measured from what each surviving "
                    "worker reported about itself, not from DataHub."
                ),
                (
                    "A worker reporting its own duplicated effort is reporting "
                    "against itself rather than flattering itself; the coverage "
                    "ledger that follows is still read independently out of DataHub."
                ),
            ]
        )
        return lines


def worker_report(run: WorkerRun) -> dict[str, object]:
    """Build the worker's JSON account, including lists and existing counts."""
    return {
        "worker_id": run.worker_id,
        "swarm_run": run.swarm_run,
        "examined": list(run.examined),
        "written": list(run.written),
        "summary": run.summary(),
    }


def write_worker_report(run: WorkerRun, path: str | Path) -> None:
    """Atomically publish one completed worker account in its target directory."""
    target = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(canonical_json(worker_report(run)))
            output.write(b"\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def discover_report_paths(
    inputs: Sequence[str | Path],
) -> tuple[list[Path], int]:
    """Expand directories and return paths plus the inferable expected count.

    Every explicit file path represents an expected worker even when that file
    is absent.  A directory can reveal only the reports currently present, so
    its expected count is necessarily the number of JSON files discovered.
    """
    paths: list[Path] = []
    expected_reports = 0
    for supplied in inputs:
        path = Path(supplied)
        if path.is_dir():
            discovered = sorted(
                candidate
                for candidate in path.iterdir()
                if candidate.is_file() and candidate.suffix == ".json"
            )
            paths.extend(discovered)
            expected_reports += len(discovered)
            continue
        paths.append(path)
        expected_reports += 1
    return paths, expected_reports


def load_reports(paths: Sequence[str | Path]) -> list[WorkerAccount]:
    """Load present reports; a missing path is an absent worker, not an error."""
    reports: list[WorkerAccount] = []
    for supplied in paths:
        path = Path(supplied)
        try:
            serialized = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        try:
            parsed = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise SwarmReportError(
                f"{path}: malformed JSON at line {error.lineno}, "
                f"column {error.colno}: {error.msg}"
            ) from error
        reports.append(parse_report(parsed, source=str(path)))
    return reports


def parse_report(value: object, *, source: str = "worker report") -> WorkerAccount:
    """Validate parsed JSON without performing I/O."""
    if not isinstance(value, Mapping):
        raise SwarmReportError(f"{source}: expected a JSON object")

    worker_id = _required_string(value, "worker_id", source)
    swarm_run = _required_string(value, "swarm_run", source)
    examined = _required_string_list(value, "examined", source)
    written = _required_string_list(value, "written", source)
    summary = value.get("summary")
    if not isinstance(summary, Mapping):
        raise SwarmReportError(f"{source}: summary must be a JSON object")

    for name in _SUMMARY_COUNTS:
        count = summary.get(name)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise SwarmReportError(
                f"{source}: summary.{name} must be a nonnegative integer"
            )
    if summary.get("worker_id") != worker_id:
        raise SwarmReportError(f"{source}: summary.worker_id does not match worker_id")
    if summary.get("swarm_run") != swarm_run:
        raise SwarmReportError(f"{source}: summary.swarm_run does not match swarm_run")
    if summary["examined"] != len(examined):
        raise SwarmReportError(f"{source}: summary.examined does not match examined")
    if summary["receipts_written"] != len(written):
        raise SwarmReportError(
            f"{source}: summary.receipts_written does not match written"
        )

    return WorkerAccount(
        worker_id=worker_id,
        swarm_run=swarm_run,
        examined=examined,
        written=written,
        summary={str(key): item for key, item in summary.items()},
    )


def measure_overlap(
    reports: Sequence[WorkerAccount],
    *,
    expected_reports: int | None = None,
) -> SwarmOverlap:
    """Purely count duplicate examinations across the surviving reports."""
    expected = len(reports) if expected_reports is None else expected_reports
    if expected < len(reports):
        raise SwarmReportError(
            f"expected {expected} reports but found {len(reports)} present reports"
        )

    worker_ids = [report.worker_id for report in reports]
    if len(set(worker_ids)) != len(worker_ids):
        raise SwarmReportError("worker ids must be unique across reports")
    swarm_runs = {report.swarm_run for report in reports}
    if len(swarm_runs) > 1:
        raise SwarmReportError("reports belong to different swarm runs")

    workers_by_asset: dict[str, set[str]] = {}
    total_examinations = 0
    for report in reports:
        total_examinations += len(report.examined)
        for urn in report.examined:
            workers_by_asset.setdefault(urn, set()).add(report.worker_id)

    overlapping_assets = {
        urn: tuple(sorted(workers))
        for urn, workers in sorted(workers_by_asset.items())
        if len(workers) > 1
    }
    distinct_assets = len(workers_by_asset)
    return SwarmOverlap(
        swarm_run=next(iter(swarm_runs), ""),
        expected_reports=expected,
        found_reports=len(reports),
        total_examinations=total_examinations,
        distinct_assets_examined=distinct_assets,
        duplicated_examinations=total_examinations - distinct_assets,
        overlapping_assets=overlapping_assets,
    )


def _required_string(value: Mapping[object, object], key: str, source: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise SwarmReportError(f"{source}: {key} must be a nonempty string")
    return item


def _required_string_list(
    value: Mapping[object, object], key: str, source: str
) -> tuple[str, ...]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise SwarmReportError(f"{source}: {key} must be a list of strings")
    return tuple(items)
