"""The phases of a detector evaluation, one at a time (R15.4).

``_evaluate`` was one 694-line function; the phases are now library calls with
frozen results. The end-to-end behaviour is pinned in
``tests/test_evaluate_detectors_script.py`` and, against the pre-split script,
by the fixed-input comparison recorded in R15; these cover the rules a whole
run exercises only on some inputs: the seen-session and default-profile skips
planned before anything loads, a drive none of whose sessions loaded, a
cross-era drive and an in-sample detector in the aggregation, and a score cache
that is settled only if no key input moved.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.detector_evaluation_inputs import EvaluationRefusedError
from src.analysis.detector_evaluation_plan import DrivePlan, LoadedSessions, render_plan
from src.analysis.detector_evaluation_run import (
    ScoredDrives,
    aggregate_results,
    settle_score_cache,
)
from src.analysis.labelled_events import LabelledEventRegistry
from src.models.evaluation import Era, SessionSpan, group_into_drives
from src.models.evaluation_cache import ScoreCache
from src.models.evaluation_identity import KeySources, key_inputs
from src.models.evaluation_records import SessionScore
from src.models.evaluation_scorer import DriveResult

T0 = datetime(2026, 5, 13, tzinfo=UTC)


def _drive(*names: str, era: Era | None = None) -> Any:
    era = era or Era("rev-a", 0)
    spans = [SessionSpan(n, T0, T0 + timedelta(seconds=60)) for n in names]
    (drive,) = group_into_drives(spans, era_of=lambda _t: era)
    return drive


def _plan(*drives: Any, **overrides: Any) -> DrivePlan:
    fields: dict[str, Any] = {
        "by_name": {n: Path(f"/sessions/{n}") for d in drives for n in d.session_names},
        "registry": LabelledEventRegistry(events=[]),
        "training_eras": frozenset({Era("rev-a", 0)}),
        "evaluation": tuple(drives),
        "skipped": ({"drive": "earlier", "sessions": ["old"], "reason": "skipped: seen"},),
        "in_sample_by_drive": {},
        "excluded": {},
        "fit_names": (),
        "calibration_names": (),
        "needed": (),
    }
    fields.update(overrides)
    return DrivePlan(**fields)


def _score(rows: int = 4) -> SessionScore:
    stamps = pd.Series(pd.date_range("2026-05-13T00:00:00Z", periods=rows, freq="1s"))
    return SessionScore(
        stamps,
        np.array([False] * rows, dtype=object),
        np.zeros(rows),
        np.zeros(rows, dtype=bool),
    )


def _result(session: str, *detectors: str, outcomes: list[dict[str, Any]] | None = None) -> Any:
    return DriveResult(
        scores={session: {name: _score() for name in detectors}},
        shift={"detectors": {name: {} for name in detectors}},
        outcomes=outcomes or [],
        skipped=[{"drive": "d", "kind": "k", "rep": 0, "reason": "no window"}],
        slices=[],
        stream_reads={},
        peak_rss_bytes=0,
        cache_counts={},
    )


def test_a_drive_none_of_whose_sessions_loaded_is_skipped_naming_each_failure() -> None:
    """The denominator is drives that were scored: one with nothing loaded is a
    named skip beside the planner's, not a zero row and not silently absent. A
    session excluded from the metrics (a tuning capture) is not a load failure."""
    drive = _drive("a", "b", "c")
    plan = _plan(drive, excluded={"c": "tuning capture"})
    loaded = LoadedSessions(frames={}, load_errors={"a": "boom: unreadable"})
    aggregate = aggregate_results(plan, loaded, ScoredDrives([], {}), ["x"])
    assert aggregate.records == []
    assert aggregate.skipped == [
        *plan.skipped,
        {
            "drive": drive.drive_id,
            "sessions": ["a", "b", "c"],
            "reason": "skipped: a: boom: unreadable; b: not loaded",
        },
    ]
    assert aggregate.outcomes == [] and aggregate.shift_runs == []


def test_a_cross_era_drive_is_recorded_but_adds_no_injection_outcome_or_slice() -> None:
    """Cross-era: recorded in an era none of the artifacts trained in. It keeps
    its clean record and shift run, and says why it has no injections."""
    drive = _drive("a", era=Era("rev-b", 1))
    plan = _plan(drive, skipped=())
    frame = pd.DataFrame(
        {"timestamp": pd.date_range("2026-05-13T00:00:00Z", periods=4, freq="1s"), "obd:RPM": 800.0}
    )
    result = _result("a", "x", outcomes=[{"detector": "x", "kind": "k"}])
    aggregate = aggregate_results(
        plan, LoadedSessions({"a": frame}, {}), ScoredDrives([result], {}), ["x"]
    )
    (record,) = aggregate.records
    assert record.cross_era is True
    assert aggregate.outcomes == [] and aggregate.slices == []
    assert aggregate.skipped_injections == [
        {
            "drive": drive.drive_id,
            "reason": "cross-era (rev-b/hw1; artifacts trained in ['rev-a/hw0'])",
        }
    ]
    assert aggregate.shift_runs == [result.shift]


def test_an_in_sample_detector_leaves_every_summary_of_its_drive() -> None:
    drive = _drive("a")
    plan = _plan(
        drive, skipped=(), in_sample_by_drive={drive.drive_id: {"y": "y budget_level on a"}}
    )
    frame = pd.DataFrame(
        {"timestamp": pd.date_range("2026-05-13T00:00:00Z", periods=4, freq="1s"), "obd:RPM": 800.0}
    )
    result = _result(
        "a", "x", "y", outcomes=[{"detector": "x", "kind": "k"}, {"detector": "y", "kind": "k"}]
    )
    aggregate = aggregate_results(
        plan, LoadedSessions({"a": frame}, {}), ScoredDrives([result], {}), ["x", "y"]
    )
    (record,) = aggregate.records
    assert record.in_sample == {"y": "y budget_level on a"}
    assert set(record.detectors) == {"x"}
    assert set(aggregate.scores["a"]) == {"x"}
    assert [o["detector"] for o in aggregate.outcomes] == ["x"]
    assert set(aggregate.shift_runs[0]["detectors"]) == {"x"}


def test_the_plan_listing_names_what_is_evaluated_skipped_and_loaded() -> None:
    drive = _drive("a", "b")
    plan = _plan(drive, excluded={"b": "tuning capture"}, needed=("a",), fit_names=("a",))
    assert render_plan(plan) == [
        f"evaluate {drive.drive_id} [rev-a/hw0]: a, b (not scored: tuning capture)",
        "skip     earlier: skipped: seen",
        "baseline fit: a",
        "baseline thresholds: none",
        "load /sessions/a",
    ]


def test_a_refusal_carries_its_stderr_lines_and_exit_status() -> None:
    refusal = EvaluationRefusedError(["error: first", "error: second"], exit_code=2)
    assert refusal.lines == ("error: first", "error: second")
    assert refusal.exit_code == 2
    assert "first" in str(refusal)


def _sources(tmp_path: Path) -> KeySources:
    (tmp_path / "profile").mkdir()
    for name in ("canary.yaml", "bindings.yaml", "behavior.yaml", "harness.py"):
        (tmp_path / name).write_text(f"{name}: 1\n")
    return KeySources(
        config_path=tmp_path / "canary.yaml",
        profile_dir=tmp_path / "profile",
        extra_data=(),
        if_path=None,
        sequence_path=None,
        bindings_path=tmp_path / "bindings.yaml",
        behavior_path=tmp_path / "behavior.yaml",
        harness_files=(tmp_path / "harness.py",),
    )


def test_a_cache_is_committed_only_if_no_key_input_moved(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    inputs = key_inputs(sources)
    root = tmp_path / "scores"

    kept = ScoreCache(root)
    kept.put("component-a", "unit-1", [1])
    assert settle_score_cache(kept, sources, inputs, started=0.0) == []
    assert ScoreCache(root).get("component-a", "unit-1") == [1]

    moved = ScoreCache(root)
    moved.put("component-a", "unit-2", [2])
    sources.bindings_path.write_text("bindings.yaml: 2\n")
    assert settle_score_cache(moved, sources, inputs, started=0.0) == ["bindings"]
    assert ScoreCache(root).get("component-a", "unit-2") is None
    assert moved.staging is not None and not moved.staging.exists()

    assert settle_score_cache(ScoreCache(None), sources, inputs, started=0.0) == []


def test_the_key_inputs_name_the_files_a_score_depends_on(tmp_path: Path) -> None:
    """Each named input moves with its own file, and only that one."""
    sources = _sources(tmp_path)
    base = key_inputs(sources)
    assert set(base) == {
        "code",
        "config_file",
        "isolation_forest",
        "sequence",
        "bindings",
        "behavior_models",
        "data",
    }
    sources.behavior_path.write_text("behavior.yaml: 2\n")
    after = key_inputs(sources)
    assert {k for k in base if base[k] != after[k]} == {"behavior_models"}
    sources.config_path.write_text("canary.yaml: 2\n")
    assert {k for k, v in key_inputs(sources).items() if base[k] != v} == {
        "behavior_models",
        "config_file",
        "data",
    }
    sources.harness_files[0].write_text("harness.py: 2\n")
    assert key_inputs(sources)["code"] != base["code"]
    assert json.dumps(sorted(base))  # the names are plain JSON-able strings


def test_the_script_hashes_its_own_files_into_the_code_identity() -> None:
    """The CLI stays outside ``src/``, so ``code_version`` takes it as an extra:
    editing the script or ``_cli.py`` invalidates every cached score."""
    from scripts import evaluate_detectors

    files = evaluate_detectors._harness_files()
    assert [p.name for p in files] == ["evaluate_detectors.py", "_cli.py"]
    assert all(p.is_file() for p in files)
