"""R20: behavior validation's rows, refusals, events, metrics and evidence are pinned.

``src/analysis/behavior_validation.py`` (2,204 lines) was split into owners
under ``src/analysis`` and kept as a facade. A pure move must not change a
refusal reason, a denominator, an event time, a metric, a synthetic outcome, a
criterion verdict or a serialized evidence field, so this runs the public
functions (through the facade) over fixed inputs and compares what comes out
with ``tests/fixtures/behavior_validation_golden.json``, captured from the
unmodified module before any code moved.

The inputs (nothing here reads a real session directory):

* a hand-built grid over a two-cell timing surface with an eligible, an
  off-map, a stale, a missing-input, a missing-actual and a deviating row,
  plus a tuning piece and an unlisted capture piece stamped by
  ``stamp_profile_exclusions``;
* per-read pairing over acquisition-clock reads: paired, beyond the bound,
  a round-stamped output, an axis never captured and a read with no grid row,
  and every ``AxisPairingRefusedError`` reason;
* the five-drive synthetic corpus from ``tests.builders`` through
  ``fit_recipe`` (publication and the broad-band refusal), and its rows
  through fold metrics, the drive bootstrap, breakdowns, regimes, synthetic
  sensitivity, transient controls, a fitted band-excess persistence and
  ``apply_criteria`` on varied metrics;
* the committed known-answer slices (``tests/fixtures/behavior_validation``)
  under the artifact they were measured on;
* the profile's validation document, the schemas, and file/session identity
  over fixed bytes.

Arrays are compared value for value (dtype, NaN mask and order included):
short ones in full, long ones by count, NaN count, extremes and a hash of
every value's ``repr``. ``evaluated_at`` and ``fitted_at`` (the run's own wall
clock) are the only fields removed.

Regenerate only on purpose, from the code whose behaviour is the reference::

    .venv/bin/python -m tests.test_behavior_validation_golden --write
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel

GOLDEN = Path(__file__).parent / "fixtures" / "behavior_validation_golden.json"
FIXTURES = Path(__file__).parent / "fixtures" / "behavior_validation"
REPO = Path(__file__).resolve().parents[1]
PROFILE_YAML = REPO / "data/nissan-xterra-2008/behavior_validation.yaml"
FULL_ARRAY = 64
#: Wall-clock stamps of the run itself (evidence and fit time).
RUN_METADATA = frozenset({"evaluated_at", "fitted_at"})

TIMING = "obd:TIMING_ADVANCE"
RPM = "obd:RPM"
LOAD = "obd:ENGINE_LOAD"
SESSION = "2026-10-01T120000"
T0 = 1_790_000_000.0


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return repr(value) if not math.isfinite(value) else value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if value is pd.NaT:
        return "NaT"
    return repr(value)


def _array(values: Any) -> dict[str, Any]:
    arr = np.asarray(values)
    items = [_scalar(v) for v in arr.tolist()] if arr.dtype != object else [_scalar(v) for v in arr]
    out: dict[str, Any] = {"dtype": str(arr.dtype), "n": len(items)}
    if len(items) <= FULL_ARRAY:
        out["values"] = items
        return out
    text = json.dumps(items)
    out["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    out["nulls"] = sum(1 for v in items if v is None or v in ("nan", "NaT"))
    numeric = [v for v in items if isinstance(v, float | int) and not isinstance(v, bool)]
    if numeric:
        out["min"], out["max"] = min(numeric), max(numeric)
    out["distinct"] = sorted({str(v) for v in items})[:20]
    return out


def _frame(frame: pd.DataFrame) -> dict[str, Any]:
    return {
        "shape": list(frame.shape),
        "columns": [str(c) for c in frame.columns],
        "dtypes": [str(t) for t in frame.dtypes],
        "index": _array(frame.index.to_numpy()),
        "data": {str(c): _array(frame[c].to_numpy()) for c in frame.columns},
    }


def _norm(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        dumped = obj.model_dump(mode="json")
        dumped.pop("evaluated_at", None)
        return _norm(dumped)
    if isinstance(obj, pd.DataFrame):
        return _frame(obj)
    if isinstance(obj, np.ndarray):
        return _array(obj)
    if isinstance(obj, dict):
        # Run metadata, at any depth: the only fields removed.
        return {str(k): _norm(v) for k, v in obj.items() if k not in RUN_METADATA}
    if isinstance(obj, (list, tuple)):
        return [_norm(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_norm(v) for v in obj)
    return _scalar(obj)


def _row_evaluation(ev: Any) -> dict[str, Any]:
    fields = {
        name: _array(getattr(ev, name))
        for name in ev.__dataclass_fields__
        if name != "axis_pairing_s"
    }
    fields["axis_pairing_s"] = _norm(ev.axis_pairing_s)
    fields["evaluated"] = _array(ev.evaluated)
    fields["eligible"] = _array(ev.eligible)
    fields["residual"] = _array(ev.residual)
    return fields


def _refusal(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the refusal is the answer
        return {
            "type": type(exc).__name__,
            "reason": getattr(exc, "reason", None),
            "message": str(exc),
        }
    return {"type": None}


def _outcome(call: Callable[[], Any]) -> Any:
    """The result, or the refusal when the call refuses."""
    try:
        return _norm(call())
    except Exception:  # noqa: BLE001 - the refusal is the answer
        return _refusal(call)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _surface() -> Any:
    from src.analysis.surface_2d import fit_grid_binned_mean

    rpm = np.repeat([1000.0, 1500.0, 2500.0, 3000.0], 50)
    load = np.tile(np.linspace(10.0, 60.0, 50), 4)
    frame = pd.DataFrame(
        {RPM: rpm, LOAD: load, TIMING: np.where(rpm < 2000.0, 20.0, 30.0), "session": "train"}
    )
    return fit_grid_binned_mean(
        frame,
        x_column=RPM,
        y_column=LOAD,
        output_column=TIMING,
        name="timing_advance_vs_rpm_load",
        output=TIMING,
        x_edges=[500.0, 2000.0, 4000.0],
        y_edges=[0.0, 100.0],
        output_unit="degBTDC",
    )


def _grid(
    seconds: list[float], rows: dict[str, list[float]], session: str = SESSION
) -> pd.DataFrame:
    from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column

    frame = pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.to_datetime([T0 + s for s in seconds], unit="s", utc=True),
            SESSION_COLUMN: session,
            MODE_COLUMN: "city",
            **rows,
        }
    )
    for channel in rows:
        frame[gap_column(channel)] = False
    return frame


def _reads(rows: list[tuple[float, str, float]], *, basis: str = "acquired") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([T0 + t for t, _, _ in rows], unit="s", utc=True),
            "spec": [spec for _, spec, _ in rows],
            "value": [value for _, _, value in rows],
            "unit": "",
            "timestamp_basis": basis,
        }
    )


def _rows_scenario(tmp: Path) -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.columns import gap_column

    model = _surface()
    n = 14
    rpm = [1000.0, 5000.0, 1200.0, 1300.0, 1400.0, 2500.0] + [1500.0] * 4 + [2600.0] * 4
    load = [30.0, 30.0, 30.0, float("nan"), 30.0, 40.0] + [30.0] * 8
    timing = [20.5, 25.0, 21.0, 20.0, float("nan"), 30.4] + [40.0] * 4 + [30.0, 31.0, 55.0, 29.0]
    grid = _grid([float(i) for i in range(n)], {TIMING: timing, RPM: rpm, LOAD: load})
    grid.loc[2, gap_column(RPM)] = True
    out: dict[str, Any] = {}
    ev = bv.evaluate_rows(model, grid)
    out["grid"] = _row_evaluation(ev)
    policy = bv.EventPolicy()
    out["grid_metrics"] = _norm(bv.fold_metrics(model, ev, drive_of_piece={}, policy=policy))
    groups = np.full(len(ev.reasons), "d", dtype=object)
    out["grid_events"] = _norm(bv.event_times(bv._verdicts(ev), ev.seconds, groups, policy))
    out["grid_count"] = bv.count_events(bv._verdicts(ev), ev.seconds, groups, policy)
    out["verdicts"] = _array(bv._verdicts(ev))

    # Profile stamps: a listed tuning piece, an unlisted capture piece, a plain one.
    tuning = "_tuning-2026-10-01T130000"
    capture = "_capture-fuel-map-2026-10-01T140000"
    mixed = pd.concat(
        [
            grid.iloc[:4],
            _grid(
                [20.0, 21.0],
                {TIMING: [20.0, 21.0], RPM: [1000.0, 1100.0], LOAD: [20.0, 20.0]},
                tuning,
            ),
            _grid(
                [30.0, 31.0],
                {TIMING: [20.0, 21.0], RPM: [1000.0, 1100.0], LOAD: [20.0, 20.0]},
                capture,
            ),
        ],
        ignore_index=True,
    )
    out["unstamped_capture"] = _refusal(lambda: bv.evaluate_rows(model, mixed))
    out["unstamped_profile_refusals"] = _refusal(lambda: bv.profile_refusals(mixed))
    sessions = [tmp / SESSION, tmp / tuning]
    out["profile_exclusions"] = _norm(bv.profile_exclusions(sessions))
    stamped = bv.stamp_profile_exclusions(mixed, sessions)
    out["stamped_column"] = _array(stamped[bv.PROFILE_EXCLUSION_COLUMN].to_numpy())
    out["stamped_refusals"] = _array(bv.profile_refusals(stamped))
    out["stamped_rows"] = _row_evaluation(bv.evaluate_rows(model, stamped))
    out["every_recording"] = _row_evaluation(
        bv.evaluate_rows(model, mixed, profile_scope="every_recording")
    )
    out["no_session_column"] = _refusal(
        lambda: bv.stamp_profile_exclusions(grid.drop(columns=["session"]), sessions)
    )
    offset = np.zeros(n)
    offset[6:10] = -15.0
    out["shaped_offset"] = _row_evaluation(bv.evaluate_rows(model, grid, output_offset=offset))
    out["offset_shape_refused"] = _refusal(
        lambda: bv.evaluate_rows(model, grid, output_offset=np.zeros(3))
    )
    out["microsecond_seconds"] = _array(
        bv._seconds(grid.assign(timestamp=grid["timestamp"].astype("datetime64[us, UTC]")), 1.0)
    )
    out["no_timestamp_seconds"] = _array(bv._seconds(grid.drop(columns=["timestamp"]), 0.5))
    return out


def _per_read_scenario() -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.behavior.behavior_models import AxisPairing

    model = _surface()
    pairing = AxisPairing(max_separation_s={RPM: 0.25, LOAD: 0.25}, basis="test")
    reads = _reads(
        [
            (0.1, TIMING, 30.0),
            (0.1, RPM, 2500.0),
            (0.1, LOAD, 40.0),
            (0.9, RPM, 1500.0),
            (0.9, LOAD, 40.0),
            (1.2, TIMING, 20.0),
            (1.3, RPM, 1100.0),
            (1.6, LOAD, 30.0),
            (2.1, TIMING, 21.0),
            (2.15, RPM, 1150.0),
            (2.2, LOAD, 30.0),
            (2.3, TIMING, 22.0),
            (2.31, RPM, 1160.0),
            (2.29, LOAD, 30.0),
            # Two rpm reads equally far from one output read: the tie-break decides.
            (5.0, TIMING, 20.0),
            (4.9, RPM, 1000.0),
            (5.1, RPM, 1800.0),
            (5.0, LOAD, 30.0),
            (9.5, TIMING, 25.0),
            (9.5, RPM, 1200.0),
            (9.5, LOAD, 30.0),
        ]
    )
    rounded = _reads([(3.4, TIMING, 20.0)], basis="round")
    reads = pd.concat([reads, rounded], ignore_index=True)
    grid = _grid(
        [0.0, 1.0, 2.0, 3.0, 5.0],
        {
            TIMING: [30.0, 20.0, 22.0, 20.0, 20.0],
            RPM: [1500.0, 1100.0, 1160.0, 1160.0, 1800.0],
            LOAD: [40.0] * 4 + [30.0],
        },
    )
    frame = bv.per_read_frame(model, grid, {SESSION: reads}, pairing, interval_seconds=1.0)
    out: dict[str, Any] = {"frame": _frame(frame)}
    ev = bv.evaluate_rows(model, frame)
    out["rows"] = _row_evaluation(ev)
    out["summary"] = _norm(bv.axis_separation_summary(frame, ev, model))
    out["metrics"] = _norm(bv.fold_metrics(model, ev, drive_of_piece={}, policy=bv.EventPolicy()))
    out["bounds"] = _norm(bv.axis_pairing_bounds(frame, model))
    out["grid_bounds"] = _norm(bv.axis_pairing_bounds(grid, model))
    no_load = reads[reads["spec"] != LOAD]
    out["not_captured"] = _frame(
        bv.per_read_frame(model, grid, {SESSION: no_load}, pairing, interval_seconds=1.0)
    )
    out["no_grid_session"] = _frame(
        bv.per_read_frame(model, grid, {"other": reads}, pairing, interval_seconds=1.0)
    )
    out["graded_grid_unpaired"] = _frame(
        bv.graded_frame(model, grid, None, bv.SessionReads(), interval_seconds=1.0)
    )
    refusals = {
        "incomplete": lambda: bv.require_axis_pairing_covers(
            model, AxisPairing(max_separation_s={RPM: 0.25}, basis="x")
        ),
        "unknown_axis": lambda: bv.require_axis_pairing_covers(
            model, AxisPairing(max_separation_s={RPM: 0.2, LOAD: 0.2, "obd:IAT": 0.2}, basis="x")
        ),
        "mixed": lambda: bv.axis_pairing_bounds(pd.concat([frame, grid], ignore_index=True), model),
        "two_policies": lambda: bv.axis_pairing_bounds(
            pd.concat(
                [
                    frame,
                    frame.assign(
                        **{
                            bv.AXIS_PAIRING_POLICY_COLUMN: json.dumps(
                                {"output": TIMING, "axes": {}}
                            )
                        }
                    ),
                ],
                ignore_index=True,
            ),
            model,
        ),
        "other_output": lambda: bv.axis_pairing_bounds(
            frame.assign(
                **{
                    bv.AXIS_PAIRING_POLICY_COLUMN: json.dumps(
                        {"output": "obd:X", "axes": {RPM: 0.25, LOAD: 0.25}}
                    )
                }
            ),
            model,
        ),
        "other_axes": lambda: bv.axis_pairing_bounds(
            frame.assign(
                **{
                    bv.AXIS_PAIRING_POLICY_COLUMN: json.dumps(
                        {"output": TIMING, "axes": {RPM: 0.25}}
                    )
                }
            ),
            model,
        ),
        "unstated": lambda: bv.axis_pairing_bounds(
            frame.drop(columns=[bv.AXIS_PAIRING_POLICY_COLUMN]), model
        ),
        "not_a_read": lambda: bv.read_model_reads(
            model.model_copy(update={"output": "derived:afr_error"}), []
        ),
        "no_reads": lambda: bv.graded_frame(
            model, grid, pairing, bv.SessionReads(), interval_seconds=1.0
        ),
        "no_session_column": lambda: bv.graded_frame(
            model,
            grid.drop(columns=["session"]),
            pairing,
            bv.SessionReads((Path(SESSION),)),
            interval_seconds=1.0,
        ),
        "missing_session": lambda: bv.graded_frame(
            model, grid, pairing, bv.SessionReads((Path("other"),)), interval_seconds=1.0
        ),
    }
    out["refusals"] = {name: _refusal(call) for name, call in refusals.items()}
    out["separation_column"] = bv.axis_separation_column(RPM)
    return out


def _corpus_scenario() -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.analysis.refit import fit_recipe
    from src.behavior.behavior_models import (
        BandExcessPersistence,
        FoldMetrics,
        TransientControlResult,
    )
    from tests.builders import (
        BEHAVIOR_T0,
        behavior_corpus,
        behavior_criteria,
        behavior_drive,
        behavior_recipe,
    )

    out: dict[str, Any] = {}
    criteria = behavior_criteria()
    corpus = behavior_corpus()
    result = fit_recipe(behavior_recipe(criteria), corpus)
    out["fit"] = {
        "skipped": result.skipped,
        "reason": result.reason,
        "model": _norm(result.model or result.candidate),
    }
    rng = np.random.default_rng(7)
    noise = corpus.assign(**{TIMING: rng.normal(20.0, 8.0, len(corpus))})
    broad = fit_recipe(behavior_recipe(criteria), noise)
    out["broad_band"] = {
        "skipped": broad.skipped,
        "reason": broad.reason,
        "model": _norm(broad.model or broad.candidate),
    }
    model = result.model or result.candidate
    assert model is not None
    policy = bv.EventPolicy()
    ev = bv.evaluate_rows(model, corpus)
    out["rows"] = _row_evaluation(ev)
    drive_of_piece = {str(p): str(p) for p in pd.unique(ev.pieces)}
    for kind in ("training_median", "training_mode_median"):
        out[f"baseline_{kind}"] = _norm(bv.fit_baseline(ev.actual, ev.modes, kind))
    baseline = bv.fit_baseline(ev.actual, ev.modes, "training_mode_median")
    out["baseline_predictions"] = _array(bv.baseline_predictions(baseline, ev.modes[:10]))
    out["baseline_empty"] = _array(bv.baseline_predictions({}, ev.modes[:3]))
    metrics = bv.fold_metrics(
        model, ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline, bootstrap_seed=3
    )
    out["metrics"] = _norm(metrics)
    out["metrics_seed0"] = _norm(
        bv.fold_metrics(model, ev, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline)
    )
    out["drive_bootstrap"] = _norm(
        bv._drive_bootstrap(np.abs(ev.residual[ev.evaluated]), ev.pieces[ev.evaluated], seed=11)
    )
    out["evaluated_hours"] = bv._evaluated_hours(
        ev.seconds, ev.evaluated, ev.pieces, interval_seconds=1.0, break_seconds=120.0
    )
    for by in ("drive", "regime"):
        out[f"breakdown_{by}"] = _norm(
            bv.breakdown(
                model, ev, by=by, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
            )
        )
    labels = np.where(ev.x < 2400.0, "low", "high").astype(object)
    out["breakdown_labels"] = _norm(
        bv.breakdown_by_labels(model, ev, labels, drive_of_piece=drive_of_piece, policy=policy)
    )
    out["envelope"] = bv.envelope_coverage(model, ev)
    out["envelope_min_rows_50"] = bv.envelope_coverage(model, ev, min_rows=50)
    out["qualifying"] = [bv.qualifying_drive_count(ev, drive_of_piece, k) for k in (1, 300, 400)]
    regimes = bv.ReportRegimes.model_validate(
        {
            "axes": {"rpm": {"channel": RPM, "unit": "rpm", "edges": [1000, 2000, 3000]}},
            "steady_state": {"max_abs_rate_per_second": {RPM: 500.0}, "basis": "test"},
        }
    )
    out["regimes"] = _norm(
        bv.regime_labels(
            corpus, regimes, revision_of_piece=lambda p: None if p.endswith("5T120000") else "rev-a"
        )
    )
    out["regimes_none"] = _norm(bv.regime_labels(corpus, None))
    sensitivity = bv.synthetic_sensitivity(model, corpus, ev, criteria, policy=policy)
    out["synthetic"] = _norm(sensitivity)
    transients = bv.transient_controls(model, corpus, ev, criteria, policy=policy)
    out["transients"] = _norm(transients)
    out["windows_60"] = _array(bv._windows(ev, 60.0))
    out["first_evaluated"] = _norm(bv._first_evaluated_seconds(ev, bv._windows(ev, 60.0)))

    # Band-excess persistence: fitted on the first drives, evented on the last.
    rule = BandExcessPersistence(reset_gap_seconds=30.0, basis="test")
    train = np.isin(ev.pieces, sorted(drive_of_piece)[:3])
    wobble = 3.0 * np.sin(np.arange(len(corpus)) / 20.0)
    train_ev = bv.evaluate_rows(model, corpus, output_offset=wobble).subset(train)
    fitted = bv.fit_band_excess_persistence(rule, train_ev, train_ev.pieces)
    out["persistence"] = _norm(fitted)
    quiet = ev.subset(train).subset(np.zeros(int(train.sum()), dtype=bool))
    out["persistence_none"] = _norm(bv.fit_band_excess_persistence(rule, quiet, quiet.pieces))
    assert not isinstance(fitted, str)
    persistent = model.model_copy(update={"event_persistence": fitted})
    shifted = bv.evaluate_rows(persistent, corpus, output_offset=-6.0)
    out["band_excess_events"] = _norm(
        bv.band_excess_event_times(shifted, shifted.pieces, fitted, dedupe_seconds=30.0)
    )
    out["model_events_persistent"] = _norm(
        bv.model_event_times(persistent, shifted, shifted.pieces, policy)
    )
    out["model_events_default"] = _norm(
        bv.model_event_times(model, shifted, shifted.pieces, policy)
    )

    # Criteria over varied metrics.
    cases: dict[str, tuple[Any, Any, list[Any], int, list[Any]]] = {
        "as_measured": (criteria, metrics, sensitivity, 5, transients),
        "no_criteria": (None, metrics, sensitivity, 5, transients),
        "no_test": (criteria, None, sensitivity, 5, transients),
        "few_drives": (criteria, metrics, sensitivity, 1, transients),
        "no_transients": (criteria, metrics, sensitivity, 5, []),
        "worse": (
            criteria,
            metrics.model_copy(
                update={
                    "coverage": 0.1,
                    "mae": 9.0,
                    "abs_error_p95": 20.0,
                    "skill_vs_baseline": 0.01,
                    "warning_half_width_median": None,
                }
            ),
            sensitivity,
            5,
            [
                TransientControlResult(
                    magnitude=6.0,
                    direction="negative",
                    duration_seconds=2.0,
                    windows=3,
                    clean_windows=0,
                    detected=0,
                    detection_fraction=None,
                )
            ],
        ),
        "rates": (
            criteria.model_copy(
                update={"max_control_events_per_hour": 0.5, "min_control_hours": 10.0}
            ),
            metrics,
            sensitivity,
            5,
            transients,
        ),
        "rates_long": (
            criteria.model_copy(
                update={"max_control_events_per_hour": 0.0, "min_control_hours": 0.0}
            ),
            metrics.model_copy(update={"events_per_hour": 3.0, "events": 2}),
            sensitivity,
            5,
            transients,
        ),
        "empty_test": (
            criteria,
            FoldMetrics(drives=0, rows=0, input_present=0, fresh=0, eligible=0, evaluated=0),
            [],
            0,
            [],
        ),
    }
    out["criteria"] = {
        name: _norm(bv.apply_criteria(c, m, s, qualifying_test_drives=q, transients=t))
        for name, (c, m, s, q, t) in cases.items()
    }

    # Evidence from a fixed split, with a taxonomy problem and provenance.
    drives = bv.group_drives(bv.piece_time_ranges(corpus))
    split = bv.chronological_split(drives, n_test=2, n_calibration=1)
    test_pieces = {p for d in split.test for p in d.pieces}
    cal_pieces = {p for d in split.calibration for p in d.pieces}
    context = bv.ValidationContext(
        decoder_identity={"bindings": "sha256:x"},
        mode_taxonomy=("city", "idle"),
        criteria_source="test.yaml",
        provenance={"sessions": ["a", "b"]},
        taxonomy_problems=("unapproved_mode:x",),
    )
    evidence, test_ev = bv.build_evidence(
        model,
        split=split,
        test_frame=corpus[corpus["session"].isin(test_pieces)],
        calibration_frame=corpus[corpus["session"].isin(cal_pieces)],
        criteria=criteria,
        baseline=baseline,
        context=context,
        tune_revision="rev-a",
    )
    out["evidence"] = _norm(evidence)
    out["evidence_rows"] = _row_evaluation(test_ev)
    out["carry_over"] = _norm(
        bv.carry_over_flags(
            persistent.model_copy(update={"evidence": evidence}),
            recipe_contract="sha256:other",
            criteria=criteria.model_copy(update={"min_validation_drives": 9}),
            context=bv.ValidationContext(
                decoder_identity={"bindings": "sha256:y"}, mode_taxonomy=("city",)
            ),
        )
    )
    out["carry_over_no_evidence"] = _norm(
        bv.carry_over_flags(
            model, recipe_contract=None, criteria=None, context=bv.ValidationContext()
        )
    )
    late = behavior_drive("2026-08-09T120000", BEHAVIOR_T0 + timedelta(days=8), seed=9)
    out["late_drive_rows"] = _row_evaluation(bv.evaluate_rows(model, late))
    return out


def _known_answer_scenario() -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.analysis.refit import default_recipes, legacy_reproduction_spec
    from src.behavior.behavior_models import load_behavior_models

    artifact = load_behavior_models(FIXTURES / "behavior_models_2026-09-22.yaml")
    document = bv.load_validation_document(PROFILE_YAML)
    assert document is not None
    catalog = {recipe.name: recipe for recipe in default_recipes("nissan-xterra-2008")}
    policy = bv.EventPolicy()
    out: dict[str, Any] = {}
    for kind in ("historical", "corrected"):
        frame = pd.concat(
            [pd.read_parquet(p) for p in sorted((FIXTURES / kind).glob("*.parquet"))],
            ignore_index=True,
        )
        # The default-profile path over the same rows: every piece named, so
        # the stamp reads each directory's own (absent) manifest.
        stamped = bv.stamp_profile_exclusions(
            frame, [FIXTURES / kind / p.stem for p in sorted((FIXTURES / kind).glob("*.parquet"))]
        )
        for model in artifact.models:
            recipe = catalog.get(model.name)
            spec = (
                legacy_reproduction_spec(recipe, model)[0]
                if recipe is not None and model.eligibility is None
                else None
            )
            try:
                ev = bv.evaluate_rows(model, frame, spec=spec, profile_scope="every_recording")
            except Exception as exc:  # noqa: BLE001
                out[f"{kind}:{model.name}"] = {"refused": f"{type(exc).__name__}: {exc}"}
                continue
            entry: dict[str, Any] = {
                "rows": _row_evaluation(ev),
                "metrics": _norm(bv.fold_metrics(model, ev, drive_of_piece={}, policy=policy)),
                "regime": _norm(
                    bv.breakdown(model, ev, by="regime", drive_of_piece={}, policy=policy)
                ),
                "labels": _norm(bv.regime_labels(frame, document.report_regimes)),
            }
            criteria = document.recipes.get(model.name)
            stamped_ev = bv.evaluate_rows(model, stamped, spec=spec)
            entry["stamped_reasons"] = _array(stamped_ev.reasons)
            if criteria is not None and stamped_ev.evaluated.any():
                entry["synthetic"] = _outcome(
                    partial(
                        bv.synthetic_sensitivity,
                        model,
                        stamped,
                        stamped_ev,
                        criteria,
                        policy=policy,
                        spec=spec,
                    )
                )
                entry["transients"] = _outcome(
                    partial(
                        bv.transient_controls,
                        model,
                        stamped,
                        stamped_ev,
                        criteria,
                        policy=policy,
                        spec=spec,
                    )
                )
                entry["unstamped_synthetic"] = _outcome(
                    partial(
                        bv.synthetic_sensitivity,
                        model,
                        frame,
                        ev,
                        criteria,
                        policy=policy,
                        spec=spec,
                    )
                )
            out[f"{kind}:{model.name}"] = entry
    return out


def _documents_scenario(tmp: Path) -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.behavior.behavior_models import ValidationEvidence
    from tests.configs import example_config

    out: dict[str, Any] = {}
    document = bv.load_validation_document(PROFILE_YAML)
    assert document is not None
    out["document"] = _norm(document)
    out["document_schema"] = bv.BehaviorValidationDocument.model_json_schema()
    out["evidence_schema"] = ValidationEvidence.model_json_schema()
    config = example_config()
    out["taxonomy_example_config"] = document.taxonomy_problems(
        config.preprocessing.operating_modes
    )
    out["taxonomy_retired"] = document.taxonomy_problems(
        [*document.approved_operating_modes[1:], *document.retired_operating_modes, "made_up"]
    )
    out["resolution_missing"] = _norm(document.resolution_for("no-such-session"))
    out["missing_document"] = _norm(bv.load_validation_document(tmp / "absent.yaml"))
    for bad, payload in {
        "both": {"session": "s", "revision": "r", "exclude": True, "reason": "x"},
        "neither": {"session": "s", "reason": "x"},
        "no_reason": {"session": "s", "exclude": True, "reason": ""},
    }.items():
        out[f"resolution_{bad}"] = _refusal(lambda p=payload: bv.ProvenanceResolution(**p))  # type: ignore[misc]
    out["regime_edges"] = _refusal(
        lambda: bv.ReportRegimes.model_validate(
            {"axes": {"a": {"channel": "c", "unit": "u", "edges": [2, 1]}}}
        )
    )
    out["extra_field"] = _refusal(
        lambda: bv.BehaviorValidationDocument.model_validate(
            {"approved_operating_modes": ["idle"], "surprise": 1}
        )
    )
    policy = bv.EventPolicy.from_config(config, document)
    out["policy"] = {
        "fields": _norm({k: getattr(policy, k) for k in policy.__dataclass_fields__}),
        "describe": policy.describe(),
        "default": _norm({k: getattr(bv.EventPolicy(), k) for k in policy.__dataclass_fields__}),
        "no_document": _norm(bv.EventPolicy.from_config(config).exposure_break_seconds),
    }
    context = bv.ValidationContext.for_run(
        config, document, decoder={"dids": "sha256:d"}, criteria_source="src.yaml"
    )
    out["context"] = _norm(
        {
            "policy": {k: getattr(context.policy, k) for k in context.policy.__dataclass_fields__},
            "decoder_identity": dict(context.decoder_identity),
            "mode_taxonomy": list(context.mode_taxonomy),
            "criteria_source": context.criteria_source,
            "join_gap_seconds": context.join_gap_seconds,
            "provenance": dict(context.provenance),
            "taxonomy_problems": list(context.taxonomy_problems),
            "reads": [str(s) for s in context.reads.sessions],
        }
    )
    out["context_no_document"] = _norm(
        bv.ValidationContext.for_run(
            config, None, decoder={}, criteria_source=None
        ).join_gap_seconds
    )
    for stamp in (
        "2026-09-20T04:00:06Z",
        "2026-09-20T04:00:07+00:00",
        "2026-09-21T00:00:00",
        "garbage",
    ):
        out[f"predates:{stamp}"] = bv.mode_labels_predate_fix(stamp)
    (tmp / "bindings.yaml").write_bytes(b"bindings: []\n")
    (tmp / "dids.yaml").write_bytes(b"dids: {}\n")
    dbc = tmp / "dbc"
    dbc.mkdir()
    (dbc / "b.dbc").write_bytes(b'VERSION "b"\n')
    (dbc / "a.dbc").write_bytes(b'VERSION "a"\n')
    (dbc / "ignored.txt").write_bytes(b"x")

    def rel(text: str) -> str:
        return text.replace(str(tmp), "<tmp>")

    out["file_identity"] = [
        rel(bv.file_identity(tmp / "dids.yaml")),
        rel(bv.file_identity(tmp / "nope")),
    ]
    out["file_sha256"] = [bv.file_sha256(tmp / "dids.yaml"), bv.file_sha256(tmp / "nope")]
    out["decoder_identity"] = [
        bv.decoder_identity(bindings_path=tmp / "bindings.yaml", dids_path=tmp / "dids.yaml"),
        bv.decoder_identity(
            bindings_path=tmp / "bindings.yaml", dids_path=tmp / "missing.yaml", dbc_dir=dbc
        ),
        bv.decoder_identity(bindings_path=tmp / "x", dids_path=tmp / "y", dbc_dir=tmp / "nodbc"),
        bv.decoder_identity(bindings_path=tmp / "x", dids_path=tmp / "y", dbc_dir=tmp),
    ]
    session = tmp / "2026-10-01T120000"
    session.mkdir()
    (session / "telemetry-000002.parquet").write_bytes(b"\x00\x01two")
    (session / "telemetry-000001.parquet").write_bytes(b"one")
    (session / "other.parquet").write_bytes(b"skip")
    out["session_identity"] = bv.session_content_identity(session)
    out["validation_document_path"] = str(
        bv.validation_document_path(Path("data/nissan-xterra-2008/behavior_models.yaml"))
    ).replace(str(REPO), "<repo>")
    out["constants"] = {
        name: getattr(bv, name)
        for name in (
            "EVALUATOR_VERSION",
            "MIN_BOOTSTRAP_DRIVES",
            "ENVELOPE_MIN_ROWS",
            "VALIDATION_FILENAME",
            "MODE_LABEL_FIX_COMMIT",
            "PROFILE_EXCLUSION_COLUMN",
            "PROFILE_EXCLUDED",
            "PROFILE_UNVERIFIED",
            "AXIS_PAIRING_COLUMN",
            "AXIS_PAIRING_POLICY_COLUMN",
            "AXIS_SEPARATION_PREFIX",
            "PERSISTENCE_NO_TRAINING_EXCURSION",
            "DEFAULT_JOIN_GAP_SECONDS",
        )
    }
    out["constants"]["MODE_LABEL_FIX_AT"] = bv.MODE_LABEL_FIX_AT.isoformat()
    out["constants"]["_PAIRABLE_BASES"] = sorted(bv._PAIRABLE_BASES)
    out["constants"]["_PROFILE_SCOPED_KINDS"] = sorted(str(k) for k in bv._PROFILE_SCOPED_KINDS)
    out["__all__"] = list(bv.__all__)
    return cast(dict[str, Any], _norm(out))


def _evaluation_frame_scenario(tmp: Path) -> dict[str, Any]:
    from src.analysis import behavior_validation as bv
    from src.behavior.behavior_models import load_behavior_models
    from src.paths import BINDINGS_YAML, DIDS_YAML
    from tests.builders import write_rich_session
    from tests.configs import example_config

    models = load_behavior_models(FIXTURES / "behavior_models_2026-09-22.yaml")
    session = write_rich_session(tmp)
    out: dict[str, Any] = {"decoded_signals": sorted(bv.evaluation_decoded_signals(models))}
    for decode in ("mode-inputs", "none"):
        frame = bv.build_evaluation_frame(
            [session],
            example_config(),
            models,
            bindings_path=BINDINGS_YAML,
            dids_path=DIDS_YAML,
            decode_can=decode,
        )
        out[decode] = _frame(frame)
    unstamped = bv.build_evaluation_frame(
        [session],
        example_config(),
        models,
        bindings_path=BINDINGS_YAML,
        dids_path=DIDS_YAML,
        profile_scope="every_recording",
    )
    out["every_recording_columns"] = [str(c) for c in unstamped.columns]
    return out


SCENARIOS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "rows": _rows_scenario,
    "per_read": lambda _tmp: _per_read_scenario(),
    "corpus": lambda _tmp: _corpus_scenario(),
    "known_answers": lambda _tmp: _known_answer_scenario(),
    "documents": _documents_scenario,
    "evaluation_frame": _evaluation_frame_scenario,
}


def _run(name: str, tmp: Path) -> Any:
    # Round-trip through JSON so the comparison sees exactly what is stored.
    return cast(dict[str, Any], json.loads(json.dumps(_norm(SCENARIOS[name](tmp)), sort_keys=True)))


def _golden() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(GOLDEN.read_text()))


def _first_difference(expected: Any, actual: Any, path: str = "") -> str | None:
    if type(expected) is not type(actual):
        return f"{path}: {expected!r} != {actual!r}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return f"{path}/{key}: present on one side only"
            found = _first_difference(expected[key], actual[key], f"{path}/{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for i, (a, b) in enumerate(zip(expected, actual, strict=True)):
            found = _first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if expected == actual else f"{path}: {expected!r} != {actual!r}"


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_behavior_validation_matches_the_pinned_baseline(name: str, tmp_path: Path) -> None:
    expected = _golden()[name]
    actual = _run(name, tmp_path)
    assert _first_difference(expected, actual) is None


def _write() -> None:
    out: dict[str, Any] = {}
    for name in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            out[name] = _run(name, Path(tmp))
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN} ({GOLDEN.stat().st_size} bytes)")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        raise SystemExit("usage: python -m tests.test_behavior_validation_golden --write")
    _write()
