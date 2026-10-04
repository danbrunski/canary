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

import pytest

from src.analysis.behavior_validation import (
    EventPolicy,
    evaluate_rows,
    load_validation_document,
    synthetic_sensitivity,
    transient_controls,
)
from src.analysis.refit import default_recipes, fit_recipe
from src.behavior.behavior_models import (
    NoiseFloor,
    SyntheticFault,
    TransientControl,
)
from src.columns import SESSION_COLUMN
from tests.builders import (
    BEHAVIOR_PROFILE_CRITERIA,
    behavior_corpus,
    behavior_criteria,
    behavior_recipe,
    behavior_resolution_spec,
)

# ---------------------------------------------------------------------------
# The spec itself refuses resolutions that cannot be tested or justified
# ---------------------------------------------------------------------------


def test_resolution_inside_the_measured_noise_is_refused() -> None:
    with pytest.raises(ValueError, match="measured noise"):
        behavior_resolution_spec(measured_noise=NoiseFloor(value=5.0, basis="measured"))


def test_transient_control_must_be_shorter_than_the_sustained_duration() -> None:
    with pytest.raises(ValueError, match="shorter than min_duration_seconds"):
        behavior_resolution_spec(
            transient_controls=[TransientControl(magnitude=6.0, duration_seconds=60.0)]
        )


def test_delay_budget_cannot_outlast_the_change() -> None:
    with pytest.raises(ValueError, match="max_detection_delay_seconds"):
        behavior_resolution_spec(max_detection_delay_seconds=90.0)


def test_resolution_must_be_the_declared_target_and_actually_injected() -> None:
    with pytest.raises(ValueError, match="must equal meaningful_deviation"):
        behavior_criteria(meaningful_deviation=3.0)
    with pytest.raises(ValueError, match="never tested"):
        behavior_criteria(
            synthetic_faults=[
                SyntheticFault(magnitude=6.0, direction="negative", duration_seconds=30)
            ]
        )


# ---------------------------------------------------------------------------
# Machinery through the real fit → evidence path
# ---------------------------------------------------------------------------


def test_sustained_shift_reports_delay_and_envelope_and_short_transient_stays_quiet() -> None:
    result = fit_recipe(behavior_recipe(behavior_criteria()), behavior_corpus())
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
    criteria = behavior_criteria(
        resolution=behavior_resolution_spec(
            transient_controls=[
                TransientControl(magnitude=6.0, direction="negative", duration_seconds=2.0),
                TransientControl(magnitude=6.0, direction="negative", duration_seconds=8.0),
            ]
        )
    )
    result = fit_recipe(behavior_recipe(criteria), behavior_corpus())
    model = result.model or result.candidate
    assert model is not None and model.evidence is not None
    evidence = model.evidence
    assert evidence.verdict == "failed"
    short, long = evidence.transient_controls
    assert short.detected == 0
    assert long.detection_fraction == pytest.approx(1.0)
    assert any(f.startswith("transient_negative_6_8s_detection=1.00>0") for f in evidence.failures)


def test_slow_detection_fails_the_delay_budget() -> None:
    frame = behavior_corpus()
    model = fit_recipe(behavior_recipe(behavior_criteria()), frame).model
    assert model is not None
    policy = EventPolicy(required=30, window=30, window_seconds=40.0)
    ev = evaluate_rows(model, frame)
    criteria = behavior_criteria(
        resolution=behavior_resolution_spec(max_detection_delay_seconds=10.0)
    )
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
    frame = behavior_corpus()
    model = fit_recipe(behavior_recipe(behavior_criteria()), frame).model
    assert model is not None
    # Make the first 10 s of every drive ineligible (engine not running).
    first = frame.groupby(SESSION_COLUMN).cumcount() < 10
    frame.loc[first, "obd:RPM"] = 0.0
    policy = EventPolicy()
    ev = evaluate_rows(model, frame)
    [case] = synthetic_sensitivity(model, frame, ev, behavior_criteria(), policy=policy)
    # Onset is the first *evaluated* row, so ineligible seconds are not delay.
    assert case.detection_delay_seconds_median == pytest.approx(2.0)


def test_missing_transient_evaluation_is_insufficient_not_passed() -> None:
    from src.analysis.behavior_validation import apply_criteria, fold_metrics

    frame = behavior_corpus()
    model = fit_recipe(behavior_recipe(behavior_criteria()), frame).model
    assert model is not None
    ev = evaluate_rows(model, frame)
    policy = EventPolicy()
    metrics = fold_metrics(model, ev, drive_of_piece={}, policy=policy)
    cases = synthetic_sensitivity(model, frame, ev, behavior_criteria(), policy=policy)
    verdict, failures = apply_criteria(
        behavior_criteria(), metrics, cases, qualifying_test_drives=5
    )
    assert verdict != "passed"
    assert "transient_controls=not_evaluated" in " ".join(failures)


# ---------------------------------------------------------------------------
# The shipped timing spec is data, and never claims knock identification
# ---------------------------------------------------------------------------


def test_profile_timing_resolution_is_declared_and_disclaims_knock() -> None:
    document = load_validation_document(BEHAVIOR_PROFILE_CRITERIA)
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
    for recipe in default_recipes("nissan-xterra-2008"):
        if recipe.output != "obd:TIMING_ADVANCE":
            continue
        claims = " ".join([*recipe.diagnostic_capabilities, recipe.notes or ""]).lower()
        assert "knock-retard activity shows" not in claims
        assert not any(
            "knock" in capability.lower() for capability in recipe.diagnostic_capabilities
        )
        assert any("knock" in limitation for limitation in recipe.diagnostic_limitations)
