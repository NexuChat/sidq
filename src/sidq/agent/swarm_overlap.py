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
    verdicts: Mapping[str, str]
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
    # Assets two or more workers examined and independently concluded the same
    # thing about.
    agreed_assets: tuple[str, ...]
    # urn -> worker_id -> examination digest, for every overlap where the
    # workers did not conclude the same thing.
    diverged_assets: Mapping[str, Mapping[str, str]]

    def summary(self) -> dict[str, object]:
        return {
            "swarm_run": self.swarm_run,
            "expected_reports": self.expected_reports,
            "found_reports": self.found_reports,
            "total_examinations": self.total_examinations,
            "distinct_assets_examined": self.distinct_assets_examined,
            "duplicated_examinations": self.duplicated_examinations,
            "overlapping_assets": dict(self.overlapping_assets),
            "cross_checked_assets": len(self.agreed_assets) + len(self.diverged_assets),
            "agreed_assets": list(self.agreed_assets),
            "diverged_assets": {
                urn: dict(digests) for urn, digests in self.diverged_assets.items()
            },
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
                verdict = "diverged" if urn in self.diverged_assets else "agreed"
                lines.append(f"  {urn}  ({', '.join(workers)}) — {verdict}")
        else:
            lines.append("  (none)")

        lines.extend(
            [
                "",
                "What the duplicated work bought:",
                (
                    f"  cross-checked assets     "
                    f"{len(self.agreed_assets) + len(self.diverged_assets)}"
                ),
                f"  independently agreed     {len(self.agreed_assets)}",
                f"  diverged                 {len(self.diverged_assets)}",
            ]
        )
        if self.diverged_assets:
            lines.append("")
            lines.append(
                "DIVERGENCE — two workers examined one asset and did not conclude"
            )
            lines.append(
                "the same thing. Either they did not read the same catalog, or the"
            )
            lines.append("engine is not deterministic. This does not say which:")
            for urn, digests in sorted(self.diverged_assets.items()):
                lines.append(f"  {urn}")
                for worker, digest in sorted(digests.items()):
                    lines.append(f"    {worker:<12} {digest[:16]}")

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
                (
                    "Agreement is evidence for determinism over the assets two "
                    "workers happened to share, not proof of it: no asset examined "
                    "once is cross-checked here at all."
                ),
            ]
        )
        return lines


def worker_report(run: WorkerRun) -> dict[str, object]:
    """Build the worker's JSON account, including lists and existing counts.

    The same invariant the parser enforces is enforced here, so the writer and
    the reader cannot disagree about what a valid report is: a run that examined
    an asset without recording what it concluded is refused at the point of
    publication rather than at the point some other process tries to read it.
    """
    if set(run.verdicts) != set(run.examined):
        raise SwarmReportError(
            f"{run.worker_id}: cannot publish a report whose verdicts do not cover "
            f"its examinations ({len(run.examined)} examined, "
            f"{len(run.verdicts)} recorded)"
        )
    return {
        "worker_id": run.worker_id,
        "swarm_run": run.swarm_run,
        "examined": list(run.examined),
        "written": list(run.written),
        "verdicts": dict(run.verdicts),
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
    verdicts = _required_verdicts(value, examined, source)
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
        verdicts=verdicts,
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
    # A run in which no worker survived produces zero duplicated examinations,
    # which is the best possible score and means nothing at all. That is not a
    # hypothetical: when every worker crashed on startup, `make swarm-demo`
    # printed "duplicated examinations 0" from a run that examined no asset.
    # An unmeasurable run has to say so rather than report a perfect one.
    if expected > 0 and not reports:
        raise SwarmReportError(
            f"no worker reports were found of {expected} expected: the run cannot "
            "be measured, and zero duplication here would mean no work was done "
            "rather than no work was repeated"
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

    # The point of the duplication. Every asset two workers reached is a chance
    # to compare two independent examinations of it; the digests are what makes
    # that comparison possible without re-running anything.
    verdicts_by_worker = {report.worker_id: report.verdicts for report in reports}
    agreed: list[str] = []
    diverged: dict[str, Mapping[str, str]] = {}
    for urn, workers in overlapping_assets.items():
        digests = {worker: verdicts_by_worker[worker][urn] for worker in workers}
        if len(set(digests.values())) == 1:
            agreed.append(urn)
        else:
            diverged[urn] = digests

    distinct_assets = len(workers_by_asset)
    return SwarmOverlap(
        swarm_run=next(iter(swarm_runs), ""),
        expected_reports=expected,
        found_reports=len(reports),
        total_examinations=total_examinations,
        distinct_assets_examined=distinct_assets,
        duplicated_examinations=total_examinations - distinct_assets,
        overlapping_assets=overlapping_assets,
        agreed_assets=tuple(agreed),
        diverged_assets=diverged,
    )


def _required_string(value: Mapping[object, object], key: str, source: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise SwarmReportError(f"{source}: {key} must be a nonempty string")
    return item


def _required_verdicts(
    value: Mapping[object, object], examined: Sequence[str], source: str
) -> Mapping[str, str]:
    """Every examined asset must carry a digest, and nothing else may.

    A report missing one is rejected rather than measured with a gap in it: a
    silently absent digest reads downstream as "these workers were not compared"
    when what happened is "this worker did not say what it concluded", and the
    difference is the whole value of the check.
    """
    items = value.get("verdicts")
    if not isinstance(items, Mapping):
        raise SwarmReportError(
            f"{source}: verdicts must be a JSON object mapping each examined "
            "asset to its examination digest"
        )
    verdicts: dict[str, str] = {}
    for urn, digest in items.items():
        if not isinstance(urn, str) or not isinstance(digest, str) or not digest:
            raise SwarmReportError(
                f"{source}: every verdict must map a URN to a nonempty digest"
            )
        verdicts[urn] = digest
    if set(verdicts) != set(examined):
        missing = sorted(set(examined) - set(verdicts))
        extra = sorted(set(verdicts) - set(examined))
        detail = []
        if missing:
            detail.append(f"{len(missing)} examined without a verdict")
        if extra:
            detail.append(f"{len(extra)} recorded for an asset it did not examine")
        raise SwarmReportError(
            f"{source}: verdicts do not cover examined ({'; '.join(detail)})"
        )
    return verdicts


def _required_string_list(
    value: Mapping[object, object], key: str, source: str
) -> tuple[str, ...]:
    items = value.get(key)
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise SwarmReportError(f"{source}: {key} must be a list of strings")
    return tuple(items)
