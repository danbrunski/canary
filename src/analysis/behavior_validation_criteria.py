"""Criteria to verdict (R20; B02.4).

:func:`apply_criteria` turns held-out metrics and synthetic outcomes into
``passed`` / ``failed`` / ``insufficient_evidence`` with every failure named;
:func:`src.behavior.behavior_models.serving_status` is the only consumer of
that verdict. It reads results, never rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from src.behavior.behavior_models import (
    FoldMetrics,
    SyntheticSensitivity,
    TransientControlResult,
    ValidationCriteria,
)


def apply_criteria(
    criteria: ValidationCriteria | None,
    test: FoldMetrics | None,
    sensitivity: Sequence[SyntheticSensitivity],
    *,
    qualifying_test_drives: int,
    transients: Sequence[TransientControlResult] = (),
) -> tuple[Literal["passed", "failed", "insufficient_evidence"], list[str]]:
    """Every criterion checked and named; no universal R² floor anywhere."""
    if criteria is None:
        return "insufficient_evidence", ["no_declared_criteria"]
    if test is None or test.evaluated == 0:
        return "insufficient_evidence", ["no_evaluated_test_rows"]
    insufficient: list[str] = []
    failed: list[str] = []
    if qualifying_test_drives < criteria.min_validation_drives:
        insufficient.append(
            f"test_drives={qualifying_test_drives}<{criteria.min_validation_drives}"
            f" (each >= {criteria.min_observations_per_drive} evaluated rows)"
        )
    if test.eligible < criteria.min_eligible_observations:
        insufficient.append(f"eligible={test.eligible}<{criteria.min_eligible_observations}")
    if (
        test.envelope_coverage is not None
        and test.envelope_coverage < criteria.min_envelope_coverage
    ):
        insufficient.append(
            f"envelope_coverage={test.envelope_coverage:.2f}<{criteria.min_envelope_coverage}"
        )
    if test.coverage is not None and test.coverage < criteria.min_coverage:
        failed.append(f"coverage={test.coverage:.2f}<{criteria.min_coverage}")
    if criteria.kind == "predictive_surface":
        assert criteria.min_skill_vs_baseline is not None
        if test.skill_vs_baseline is None:
            insufficient.append("skill_vs_baseline=unavailable (no training baseline)")
        elif test.skill_vs_baseline < criteria.min_skill_vs_baseline:
            failed.append(
                f"skill_vs_baseline={test.skill_vs_baseline:.3f}<{criteria.min_skill_vs_baseline}"
            )
    # Physical resolution, for every family: a typical error larger than the
    # deviation the model must resolve means it cannot resolve it.
    if test.mae is not None and test.mae > criteria.meaningful_deviation:
        failed.append(f"mae={test.mae:.4g}>meaningful_deviation={criteria.meaningful_deviation}")
    if criteria.max_mae is not None and test.mae is not None and test.mae > criteria.max_mae:
        failed.append(f"mae={test.mae:.4g}>{criteria.max_mae}")
    if (
        criteria.max_abs_error_p95 is not None
        and test.abs_error_p95 is not None
        and test.abs_error_p95 > criteria.max_abs_error_p95
    ):
        failed.append(f"abs_error_p95={test.abs_error_p95:.4g}>{criteria.max_abs_error_p95}")
    width = test.warning_half_width_median
    if width is None:
        failed.append("warning_half_width=unavailable")
    elif width > criteria.meaningful_deviation:
        failed.append(
            f"warning_half_width_median={width:.4g}>meaningful_deviation="
            f"{criteria.meaningful_deviation}"
        )
    if criteria.max_control_events_per_hour is not None and test.events_per_hour is not None:
        if test.evaluated_hours < criteria.min_control_hours:
            insufficient.append(
                f"control_hours={test.evaluated_hours:.2f}<{criteria.min_control_hours} "
                f"({test.events} event(s) is not a rate)"
            )
        elif test.events_per_hour > criteria.max_control_events_per_hour:
            failed.append(
                f"control_events_per_hour={test.events_per_hour:.3g}>"
                f"{criteria.max_control_events_per_hour}"
            )
    for case in sensitivity:
        label = f"synthetic_{case.direction}_{case.magnitude:g}_{case.duration_seconds:g}s"
        if case.detection_fraction is None:
            insufficient.append(f"{label}=no_clean_windows ({case.windows} scored)")
        elif case.detection_fraction < criteria.min_synthetic_detection:
            failed.append(
                f"{label}_detection={case.detection_fraction:.2f}<{criteria.min_synthetic_detection}"
            )
    resolution = criteria.resolution
    if resolution is not None:
        for case in sensitivity:
            if (
                case.magnitude < resolution.sustained_change
                or case.duration_seconds < resolution.min_duration_seconds
                or case.detection_delay_seconds_median is None
            ):
                continue
            label = f"synthetic_{case.direction}_{case.magnitude:g}_{case.duration_seconds:g}s"
            if case.detection_delay_seconds_median > resolution.max_detection_delay_seconds:
                failed.append(
                    f"{label}_delay_median={case.detection_delay_seconds_median:.3g}s>"
                    f"{resolution.max_detection_delay_seconds}s"
                )
        if not transients:
            insufficient.append("transient_controls=not_evaluated")
        for result in transients:
            label = (
                f"transient_{result.direction}_{result.magnitude:g}_{result.duration_seconds:g}s"
            )
            if result.detection_fraction is None:
                insufficient.append(f"{label}=no_clean_windows ({result.windows} scored)")
            elif result.detection_fraction > resolution.max_transient_detection:
                failed.append(
                    f"{label}_detection={result.detection_fraction:.2f}>"
                    f"{resolution.max_transient_detection}"
                )
    pairing = criteria.axis_pairing
    if pairing is not None:
        # B04.9 — a verdict on grid-joined rows answers a different question:
        # the axes may describe an instant seconds from the graded read.
        graded = test.axis_pairing_s
        if graded is None:
            failed.append(
                "axis_pairing=grid (criteria require each output read paired with its axes "
                f"within {pairing.max_separation_s} s)"
            )
        else:
            for axis, bound in sorted(pairing.max_separation_s.items()):
                got = graded.get(axis)
                if got is None or got > bound:
                    failed.append(f"axis_pairing:{axis}={got}s>{bound}s")
    if failed:
        return "failed", failed + [f"insufficient:{item}" for item in insufficient]
    if insufficient:
        return "insufficient_evidence", insufficient
    return "passed", []
