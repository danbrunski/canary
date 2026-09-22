"""B04.3 — a declared useful resolution, tested through the real evaluator.

The resolution spec is profile data (``behavior_validation.yaml``): the
sustained change a model must resolve, for how long, the measured noise that
justifies it, and the transient excursions it must *not* alert on. These
tests exercise the machinery only — synthetic step faults and synthetic
transient controls on a synthetic corpus, scored through the same B01
eligibility and daemon persistence gate as every other evidence number.
Nothing here is real fault recall, and nothing is fitted to real drives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.behavior_validation import (
    EventPolicy,
    evaluate_rows,
    load_validation_document,
    synthetic_sensitivity,
    transient_controls,
)
from src.analysis.refit import Surface2DRecipe, default_recipes, fit_recipe
from src.decode.behavior_models import (
    DeviationConfig,
    NoiseFloor,
    ResolutionSpec,
    SyntheticFault,
    TransientControl,
    ValidationCriteria,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
PROFILE_CRITERIA = Path("data/nissan-xterra-2008/behavior_validation.yaml")


def _resolution(**overrides: Any) -> ResolutionSpec:
    fields: dict[str, Any] = {
        "sustained_change": 5.0,
        "min_duration_seconds": 60.0,
        "max_detection_delay_seconds": 10.0,
        "measured_noise": NoiseFloor(value=0.4, basis="synthetic corpus: generated sigma"),
        "diagnostic_question": "did the output shift by a sustained 5 units?",
        "claim": "unexpected sustained change versus the learned surface",
        "not_identified": ["the cause of the change"],
        "transient_controls": [
            TransientControl(magnitude=6.0, direction="negative", duration_seconds=2.0)
        ],
    }
    fields.update(overrides)
    return ResolutionSpec(**fields)


def _criteria(**overrides: Any) -> ValidationCriteria:
    fields: dict[str, Any] = {
        "kind": "predictive_surface",
        "unit": "deg",
        "noise_floor": NoiseFloor(value=0.5, basis="test LSB"),
        "meaningful_deviation": 5.0,
        "meaningful_deviation_basis": "test",
        "min_validation_drives": 2,
        "min_eligible_observations": 100,
        "min_envelope_coverage": 0.0,
        "min_skill_vs_baseline": 0.3,
        "synthetic_faults": [
            SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=60)
        ],
        "resolution": _resolution(),
    }
    fields.update(overrides)
    return ValidationCriteria(**fields)


def _drive(name: str, start: datetime, *, n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rpm = rng.uniform(800, 4000, n)
    load = rng.uniform(10, 90, n)
    output = 10.0 + 0.004 * rpm - 0.1 * load + rng.normal(0.0, 0.4, n)
    return pd.DataFrame(
        {
            TIMESTAMP_COLUMN: pd.date_range(start, periods=n, freq="1s", tz="UTC"),
            SESSION_COLUMN: name,
            MODE_COLUMN: "city",
            "obd:RPM": rpm,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": output,
        }
    )


def _corpus() -> pd.DataFrame:
    names = [f"2026-08-0{d + 1}T120000" for d in range(5)]
    return pd.concat(
        [_drive(name, T0 + timedelta(days=day), seed=day) for day, name in enumerate(names)],
        ignore_index=True,
    )


def _recipe(criteria: ValidationCriteria) -> Surface2DRecipe:
    return Surface2DRecipe(
        name="timing_test",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        x_edges=(800.0, 1600.0, 2400.0, 3200.0, 4000.0),
        y_edges=(10.0, 30.0, 50.0, 70.0, 90.0),
        min_samples=200,
        min_cell_count=5,
        min_rpm=400.0,
        max_validation_warning_rate=1.0,
        max_validation_critical_rate=1.0,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
        validation=criteria,
    )


# ---------------------------------------------------------------------------
# The spec itself refuses resolutions that cannot be tested or justified
# ---------------------------------------------------------------------------


def test_resolution_inside_the_measured_noise_is_refused() -> None:
    with pytest.raises(ValueError, match="measured noise"):
        _resolution(measured_noise=NoiseFloor(value=5.0, basis="measured"))


def test_transient_control_must_be_shorter_than_the_sustained_duration() -> None:
    with pytest.raises(ValueError, match="shorter than min_duration_seconds"):
        _resolution(transient_controls=[TransientControl(magnitude=6.0, duration_seconds=60.0)])


def test_delay_budget_cannot_outlast_the_change() -> None:
    with pytest.raises(ValueError, match="max_detection_delay_seconds"):
        _resolution(max_detection_delay_seconds=90.0)


def test_resolution_must_be_the_declared_target_and_actually_injected() -> None:
    with pytest.raises(ValueError, match="must equal meaningful_deviation"):
        _criteria(meaningful_deviation=3.0)
    with pytest.raises(ValueError, match="never tested"):
        _criteria(
            synthetic_faults=[
                SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=30)
            ]
        )


# ---------------------------------------------------------------------------
# Machinery through the real fit → evidence path
# ---------------------------------------------------------------------------


def test_sustained_shift_reports_delay_and_envelope_and_short_transient_stays_quiet() -> None:
    result = fit_recipe(_recipe(_criteria()), _corpus())
    assert result.model is not None, result.reason
    evidence = result.model.evidence
    assert evidence is not None
    assert evidence.verdict == "passed", evidence.failures
    [sustained] = evidence.sensitivity
    assert sustained.synthetic is True
    assert sustained.detection_fraction == pytest.approx(1.0)
    # 3-of-5 persistence at 1 Hz: the third shifted tick emits → 2 s after onset.
    assert sustained.detection_delay_seconds_median == pytest.approx(2.0)
    assert sustained.detection_delay_seconds_max is not None
    assert sustained.detection_delay_seconds_max <= 10.0
    assert sustained.covered_envelope is not None and sustained.covered_envelope > 0.5
    # Unmodified held-out drives: the no-fault control never alerted.
    assert sustained.control_detected == 0
    [transient] = evidence.transient_controls
    assert transient.synthetic is True
    assert transient.clean_windows > 0
    assert transient.detected == 0 and transient.detection_fraction == 0.0


def test_transient_longer_than_the_persistence_gate_fails_the_resolution() -> None:
    criteria = _criteria(
        resolution=_resolution(
            transient_controls=[
                TransientControl(magnitude=6.0, direction="negative", duration_seconds=2.0),
                TransientControl(magnitude=6.0, direction="negative", duration_seconds=8.0),
            ]
        )
    )
    result = fit_recipe(_recipe(criteria), _corpus())
    model = result.model or result.candidate
    assert model is not None and model.evidence is not None
    evidence = model.evidence
    assert evidence.verdict == "failed"
    short, long = evidence.transient_controls
    assert short.detected == 0
    assert long.detection_fraction == pytest.approx(1.0)
    assert any(f.startswith("transient_negative_6_8s_detection=1.00>0") for f in evidence.failures)


def test_slow_detection_fails_the_delay_budget() -> None:
    frame = _corpus()
    model = fit_recipe(_recipe(_criteria()), frame).model
    assert model is not None
    policy = EventPolicy(required=30, window=30, window_seconds=40.0)
    ev = evaluate_rows(model, frame)
    criteria = _criteria(resolution=_resolution(max_detection_delay_seconds=10.0))
    [case] = synthetic_sensitivity(model, frame, ev, criteria, policy=policy)
    assert case.detection_delay_seconds_median == pytest.approx(29.0)
    from src.analysis.behavior_validation import apply_criteria, fold_metrics

    metrics = fold_metrics(model, ev, drive_of_piece={}, policy=policy)
    transients = transient_controls(model, frame, ev, criteria, policy=policy)
    _verdict, failures = apply_criteria(
        criteria, metrics, [case], qualifying_test_drives=5, transients=transients
    )
    assert any("_delay_median=29s>10.0s" in failure for failure in failures)


def test_refused_rows_are_not_trials_and_delay_counts_from_visible_onset() -> None:
    frame = _corpus()
    model = fit_recipe(_recipe(_criteria()), frame).model
    assert model is not None
    # Make the first 10 s of every drive ineligible (engine not running).
    first = frame.groupby(SESSION_COLUMN).cumcount() < 10
    frame.loc[first, "obd:RPM"] = 0.0
    policy = EventPolicy()
    ev = evaluate_rows(model, frame)
    [case] = synthetic_sensitivity(model, frame, ev, _criteria(), policy=policy)
    # Onset is the first *evaluated* row, so ineligible seconds are not delay.
    assert case.detection_delay_seconds_median == pytest.approx(2.0)


def test_missing_transient_evaluation_is_insufficient_not_passed() -> None:
    from src.analysis.behavior_validation import apply_criteria, fold_metrics

    frame = _corpus()
    model = fit_recipe(_recipe(_criteria()), frame).model
    assert model is not None
    ev = evaluate_rows(model, frame)
    policy = EventPolicy()
    metrics = fold_metrics(model, ev, drive_of_piece={}, policy=policy)
    cases = synthetic_sensitivity(model, frame, ev, _criteria(), policy=policy)
    verdict, failures = apply_criteria(_criteria(), metrics, cases, qualifying_test_drives=5)
    assert verdict != "passed"
    assert "transient_controls=not_evaluated" in " ".join(failures)


# ---------------------------------------------------------------------------
# The shipped timing spec is data, and never claims knock identification
# ---------------------------------------------------------------------------


def test_profile_timing_resolution_is_declared_and_disclaims_knock() -> None:
    document = load_validation_document(PROFILE_CRITERIA)
    assert document is not None
    criteria = document.recipes["timing_advance_vs_rpm_load"]
    resolution = criteria.resolution
    assert resolution is not None
    assert resolution.sustained_change == criteria.meaningful_deviation
    assert "knock" not in resolution.claim.lower()
    assert any("knock" in item.lower() for item in resolution.not_identified)
    # Justified by a measured spread, above (not equal to) the quantization floor.
    assert "measured" in resolution.measured_noise.basis.lower()
    assert resolution.measured_noise.value > criteria.noise_floor.value
    # A transient control shorter than the sustained duration is declared.
    assert resolution.transient_controls
    assert all(
        control.duration_seconds < resolution.min_duration_seconds
        for control in resolution.transient_controls
    )


def test_timing_recipes_do_not_claim_knock_identification() -> None:
    for recipe in default_recipes():
        if recipe.output != "obd:TIMING_ADVANCE":
            continue
        claims = " ".join([*recipe.diagnostic_capabilities, recipe.notes or ""]).lower()
        assert "knock-retard activity shows" not in claims
        assert not any(
            "knock" in capability.lower() for capability in recipe.diagnostic_capabilities
        )
        assert any("knock" in limitation for limitation in recipe.diagnostic_limitations)
