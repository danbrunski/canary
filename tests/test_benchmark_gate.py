"""R06.5: the regression view of an evaluate_detectors report, which retrain's
benchmark gate and the promotion gate both read (the synthetic-fault grid it
replaces is retired)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.models.benchmark_gate import (
    DEFAULT_TOLERANCE,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    RetiredProtocolError,
    diff_reports,
    gate_view,
    incomparable_reason,
    regression_summary,
    sequence_backend_for_artifact,
)


def _recall_block(
    detected: int, graded: int, ttd: float | None = 10.0, **extra: int
) -> dict[str, Any]:
    return {
        "recall": {"estimate": detected / graded if graded else None},
        "injections": graded + sum(extra.values()),
        "graded": graded,
        "detected": detected,
        "withheld": extra.get("withheld", 0),
        "masked": extra.get("masked", 0),
        "median_time_to_detect_seconds": ttd if detected else None,
    }


def report(
    *,
    if_detected: int = 4,
    if_graded: int = 4,
    if_episodes: int = 1,
    ttd: float = 10.0,
    drives: tuple[str, ...] = ("2026-09-23T150658",),
) -> dict[str, Any]:
    """A minimal evaluate_detectors report: the fields the gate reads."""
    return {
        "protocol": {
            "name": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "seed": 42,
            "faults": ["maf_gain"],
            "injections_per_drive": 2,
            "window_seconds": 240.0,
            "lead_seconds": 240.0,
            "grace_seconds": 60.0,
            "persistence": {"required": 3, "window": 5, "window_seconds": 10.0},
        },
        "detectors": ["isolation_forest", "sequence_global", "robust_z"],
        "withheld_detectors": {"fused": "min_agreement 2 > 1 source", "robust_z": "x"},
        "drives": [
            {
                "drive": drive,
                "sessions": [drive],
                "cross_era": False,
                "engine_hours": 0.5,
                "detectors": {
                    "isolation_forest": {"false_alarm_episodes": if_episodes},
                    "sequence_global": {"false_alarm_episodes": 9},
                    "robust_z": {"false_alarm_episodes": 9},
                },
            }
            for drive in drives
        ]
        + [
            {
                "drive": "cross",
                "sessions": ["cross"],
                "cross_era": True,
                "engine_hours": 10.0,
                "detectors": {"isolation_forest": {"false_alarm_episodes": 100}},
            }
        ],
        "recall": {
            "isolation_forest": {
                "maf_gain": {
                    "0.1": _recall_block(if_detected, if_graded, ttd),
                    "0.25": _recall_block(2, 2, ttd),
                }
            },
            "robust_z": {"maf_gain": {"0.1": _recall_block(0, 4)}},
        },
    }


def test_gate_view_grades_the_served_stack_only() -> None:
    view = gate_view(report())
    assert {row["detector"] for row in view.table} == {"isolation_forest"}
    first = view.table[0]
    assert (first["fault_kind"], first["severity"], first["recall"]) == ("maf_gain", 0.1, 1.0)
    # Episodes per engine-hour over the in-era drives only: 1 / 0.5 h.
    assert view.nominal_fp["isolation_forest"]["per_hour"] == pytest.approx(2.0)
    assert view.nominal_fp["isolation_forest"]["drives"] == 1
    assert "sequence_global" not in view.nominal_fp and "robust_z" not in view.nominal_fp
    assert view.withheld == {"fused": "min_agreement 2 > 1 source"}


def test_gate_view_says_none_not_zero_when_nothing_was_graded() -> None:
    r = report()
    r["recall"]["isolation_forest"]["maf_gain"]["0.1"] = _recall_block(0, 0, masked=2)
    for drive in r["drives"]:
        drive["engine_hours"] = 0.0
    view = gate_view(r)
    assert view.table[0]["recall"] is None and view.table[0]["masked"] == 2
    assert view.nominal_fp["isolation_forest"]["per_hour"] is None


def test_a_grid_era_report_is_refused_by_name() -> None:
    grid = {"generated_at": "20260101-000000", "table": [], "cells": [], "nominal_fp": {}}
    with pytest.raises(RetiredProtocolError, match="retired detector_benchmark grid"):
        gate_view(grid)
    assert "retired" in (incomparable_reason(grid, report()) or "")
    diff = diff_reports(grid, report())
    assert diff["comparable"] is False and diff["regressed"] is False


def test_same_report_is_comparable_and_not_regressed() -> None:
    diff = diff_reports(report(), report())
    assert diff["comparable"] is True and diff["regressed"] is False
    assert {c["status"] for c in diff["cells"]} == {"compared"}
    assert regression_summary(diff) == []


def test_recall_drop_beyond_tolerance_regresses() -> None:
    diff = diff_reports(report(if_detected=4), report(if_detected=2))
    assert diff["regressed"] is True
    (line,) = regression_summary(diff)
    assert "isolation_forest maf_gain @0.1: recall 1 -> 0.5" in line


def test_latency_and_false_alarm_increases_regress() -> None:
    assert diff_reports(report(ttd=10.0), report(ttd=20.0))["regressed"] is True
    diff = diff_reports(report(if_episodes=1), report(if_episodes=3))
    assert diff["regressed"] is True
    assert any("false_alarms: per_hour 2 -> 6" in line for line in regression_summary(diff))


def test_within_tolerance_is_not_flagged() -> None:
    assert DEFAULT_TOLERANCE == 0.15
    base = report(if_detected=10, if_graded=10)
    assert diff_reports(base, report(if_detected=9, if_graded=10))["regressed"] is False


def test_a_cell_the_baseline_graded_and_the_current_run_did_not_regresses() -> None:
    """R04.5 semantics in the regression diff too: a detector that stopped
    being graded lost coverage. The grid recorded it as ``removed`` and passed."""
    current = report()
    del current["recall"]["isolation_forest"]["maf_gain"]["0.25"]
    diff = diff_reports(report(), current)
    assert diff["regressed"] is True
    missing = [c for c in diff["cells"] if c["status"] == "missing"]
    assert [(c["detector"], c["severity"]) for c in missing] == [("isolation_forest", 0.25)]

    withheld = report()
    withheld["detectors"] = ["robust_z"]
    assert diff_reports(report(), withheld)["regressed"] is True


def test_a_cell_only_the_current_run_graded_is_added_not_regressed() -> None:
    base = report()
    del base["recall"]["isolation_forest"]["maf_gain"]["0.25"]
    diff = diff_reports(base, report())
    assert diff["regressed"] is False
    assert [c["status"] for c in diff["cells"] if c["severity"] == 0.25] == ["added"]


def test_other_drives_parameters_or_holdouts_are_incomparable() -> None:
    other = report(drives=("2026-09-23T162918",))
    assert "scored different drives" in (incomparable_reason(report(), other) or "")

    reseeded = report()
    reseeded["protocol"]["seed"] = 7
    assert "seed differs" in (incomparable_reason(report(), reseeded) or "")

    bumped = copy.deepcopy(report())
    bumped["protocol"]["version"] = PROTOCOL_VERSION + 1
    assert "protocol version" in (incomparable_reason(report(), bumped) or "")

    a, b = report(), report()
    a["holdout"] = {"evaluation": ["x"]}
    b["holdout"] = {"evaluation": ["y"]}
    assert "different evaluation holdouts" in (incomparable_reason(a, b) or "")


def test_the_diff_survives_a_json_round_trip() -> None:
    once = json.loads(json.dumps(report()))
    assert diff_reports(once, report())["comparable"] is True


def test_sequence_backend_comes_from_the_artifact_manifest(tmp_path: Path) -> None:
    tcn = tmp_path / "model.pt"
    tcn.with_suffix(".manifest.json").write_text(json.dumps({"model_type": "tcn_autoencoder"}))
    assert sequence_backend_for_artifact(tcn) == "tcn"
    lstm = tmp_path / "lstm" / "model.pt"
    lstm.parent.mkdir()
    lstm.with_suffix(".manifest.json").write_text(json.dumps({"model_type": "lstm_autoencoder"}))
    assert sequence_backend_for_artifact(lstm) == "lstm"
    assert sequence_backend_for_artifact(tmp_path / "absent.pt") is None
