"""Selection and measurement must not happen on the same rows.

The reader's published figure used to come from the held-out set that had also
chosen the head and the threshold. A number that is the maximum of a search over
the rows it is then reported on is an upper bound of that search, not a
measurement, and the gap is not hypothetical: the same corpus gave 95.8%/58.0%
under the old arrangement and 97.1%/55.5% once selection moved off those rows.

These tests hold the separation, not the numbers. The numbers live in
`data/claims/reader/report.json` and are guarded against the documents by
`test_published_claims.py`; what is guarded here is that the split exists, that
it is deterministic, and that nothing searches on the rows the figure comes from.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "claims" / "reader" / "report.json"


def _script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "train_claim_reader", ROOT / "scripts" / "train_claim_reader.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_calibration_split_is_deterministic_without_a_seed() -> None:
    """No RNG to pin means the same split on any machine, in any Python."""
    numpy = pytest.importorskip("numpy")
    labels = numpy.array([0] * 20 + [1] * 12 + [2] * 8)
    first = _script()._stratified_quarter(labels)
    second = _script()._stratified_quarter(labels)
    assert (first[0] == second[0]).all()
    assert (first[1] == second[1]).all()


def test_every_label_reaches_the_calibration_split() -> None:
    """Stratified by construction: a rare label cannot be absent from it by luck.

    A threshold calibrated where a proposable label never appears would be
    calibrated against a question it was never asked.
    """
    numpy = pytest.importorskip("numpy")
    labels = numpy.array([0] * 20 + [1] * 12 + [2] * 8)
    _, calibration = _script()._stratified_quarter(labels)
    assert set(labels[calibration].tolist()) == {0, 1, 2}


def test_the_two_splits_partition_the_rows() -> None:
    numpy = pytest.importorskip("numpy")
    labels = numpy.array([0] * 20 + [1] * 12)
    fit, calibration = _script()._stratified_quarter(labels)
    assert (fit ^ calibration).all()
    assert int(calibration.sum()) == 8


def test_measure_at_does_not_search_for_a_better_threshold() -> None:
    """The published figure is measured, not maximised.

    `_operating_point` searches, which is right on calibration rows and wrong on
    the rows a figure is reported from. This asserts the reporting path takes the
    threshold it is given even when a better one exists.
    """
    numpy = pytest.importorskip("numpy")
    module = _script()
    probabilities = numpy.array([[0.1, 0.9], [0.4, 0.6]])
    truth = numpy.array([1, 0])
    generous = module._measure_at(probabilities, truth, [1], 0.5)
    strict = module._measure_at(probabilities, truth, [1], 0.8)
    assert generous["threshold"] == 0.5
    assert strict["threshold"] == 0.8
    assert generous["precision"] < strict["precision"]


def test_the_report_records_all_three_split_sizes() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    for field in ("train_rows", "calibration_rows", "eval_rows", "refit_rows"):
        assert isinstance(report.get(field), int), f"{field} missing from the report"
    assert report["refit_rows"] == report["train_rows"] + report["calibration_rows"]


def test_the_published_point_uses_the_threshold_calibration_chose() -> None:
    """The one property that makes the figure a measurement rather than a ceiling."""
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert (
        report["operating_point"]["threshold"]
        == report["calibration_point"]["threshold"]
    )


def test_the_calibration_point_is_kept_beside_the_published_one() -> None:
    """Both are published so the gap between them is visible rather than absorbed."""
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert "calibration_point" in report
    assert "selection_note" in report
    assert "eval_rows" in str(report["selection_note"])


def test_the_report_says_whether_the_challenger_could_run() -> None:
    """CatBoost is in no lockfile, so `--fit` must say when the ladder was short.

    Silently scoring one candidate and reporting a choice would describe a
    comparison that did not happen.
    """
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert isinstance(report.get("challenger_available"), bool)
