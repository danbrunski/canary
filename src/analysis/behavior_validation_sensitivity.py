"""Synthetic sensitivity and transient controls (R20; B02.6/B04.3).

Explicitly synthetic step faults and short excursions, injected into the
recorded rows and scored through the same eligibility and persistence gate as
the recording. Synthetic detection is never called fault recall.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.behavior_validation_evaluation import (
    EventPolicy,
    RowEvaluation,
    evaluate_rows,
    model_event_times,
)
from src.analysis.behavior_validation_metrics import envelope_coverage
from src.behavior.behavior_eligibility import ServingSpec
from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    SyntheticSensitivity,
    TransientControlResult,
    ValidationCriteria,
)


def synthetic_sensitivity(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    base: RowEvaluation,
    criteria: ValidationCriteria,
    *,
    policy: EventPolicy,
    spec: ServingSpec | None = None,
) -> list[SyntheticSensitivity]:
    """Detection of each declared synthetic step on non-overlapping windows.

    Every ``duration_seconds`` window of each piece holding at least one
    evaluated row is scored twice with a fresh gate (no carried hits): as
    recorded (the no-fault control) and with the step added. Only windows
    whose control stayed quiet are trials — a window that alerts anyway
    cannot show the injection was what the detector saw.
    """
    out: list[SyntheticSensitivity] = []
    for fault in criteria.synthetic_faults:
        offset = fault.magnitude if fault.direction == "positive" else -fault.magnitude
        injected = evaluate_rows(
            model,
            frame,
            spec=spec,
            interval_seconds=policy.interval_seconds,
            output_offset=offset,
        )
        windows = _windows(base, fault.duration_seconds)
        valid = windows >= 0
        first_events = _first_event_per_window(model, injected, windows, valid, policy)
        detected = set(first_events)
        control = _windows_with_event(model, base, windows, valid, policy)
        scored = {int(w) for w, ok in zip(windows, valid, strict=True) if ok}
        scored = {w for w in scored if base.evaluated[windows == w].any()}
        control &= scored
        clean = scored - control
        detected &= clean
        onsets = _first_evaluated_seconds(base, windows)
        delays = np.asarray([first_events[w] - onsets[w] for w in sorted(detected)], dtype=float)
        clean_rows = np.isin(windows, sorted(clean)) & base.evaluated
        out.append(
            SyntheticSensitivity(
                magnitude=fault.magnitude,
                direction=fault.direction,
                duration_seconds=fault.duration_seconds,
                windows=len(scored),
                control_detected=len(control),
                clean_windows=len(clean),
                detected=len(detected),
                detection_fraction=(len(detected) / len(clean)) if clean else None,
                detection_delay_seconds_median=float(np.median(delays)) if delays.size else None,
                detection_delay_seconds_max=float(delays.max()) if delays.size else None,
                covered_envelope=(
                    envelope_coverage(model, base.subset(clean_rows)) if clean else None
                ),
            )
        )
    return out


def transient_controls(
    model: BehaviorModel1D | BehaviorModel2D,
    frame: pd.DataFrame,
    base: RowEvaluation,
    criteria: ValidationCriteria,
    *,
    policy: EventPolicy,
    spec: ServingSpec | None = None,
) -> list[TransientControlResult]:
    """Events raised by each declared *short* synthetic excursion (B04.3).

    Windows are the resolution's ``min_duration_seconds``. In each window
    holding an evaluated row whose unmodified control stayed quiet, the
    excursion is added for ``duration_seconds`` starting at the window's first
    evaluated row, then scored through the same eligibility and persistence
    gate. An event is the model mistaking a transient for the sustained
    change it is meant to resolve.
    """
    resolution = criteria.resolution
    if resolution is None:
        return []
    windows = _windows(base, resolution.min_duration_seconds)
    valid = windows >= 0
    scored = {int(w) for w, ok in zip(windows, valid, strict=True) if ok}
    scored = {w for w in scored if base.evaluated[windows == w].any()}
    control = _windows_with_event(model, base, windows, valid, policy) & scored
    clean = scored - control
    onsets = _first_evaluated_seconds(base, windows)
    out: list[TransientControlResult] = []
    for case in resolution.transient_controls:
        sign = 1.0 if case.direction == "positive" else -1.0
        offset = np.zeros(len(base.seconds), dtype=float)
        for window in clean:
            rows = windows == window
            start = onsets[window]
            within = rows & (base.seconds >= start) & (base.seconds < start + case.duration_seconds)
            offset[within] = sign * case.magnitude
        injected = evaluate_rows(
            model, frame, spec=spec, interval_seconds=policy.interval_seconds, output_offset=offset
        )
        detected = _windows_with_event(model, injected, windows, valid, policy) & clean
        out.append(
            TransientControlResult(
                magnitude=case.magnitude,
                direction=case.direction,
                duration_seconds=case.duration_seconds,
                windows=len(scored),
                clean_windows=len(clean),
                detected=len(detected),
                detection_fraction=(len(detected) / len(clean)) if clean else None,
            )
        )
    return out


def _first_evaluated_seconds(ev: RowEvaluation, windows: np.ndarray) -> dict[int, float]:
    """Per window, the time of its first evaluated row (the visible onset)."""
    out: dict[int, float] = {}
    evaluated = ev.evaluated & (windows >= 0) & np.isfinite(ev.seconds)
    for window in pd.unique(windows[evaluated]):
        out[int(window)] = float(np.min(ev.seconds[evaluated & (windows == window)]))
    return out


def _first_event_per_window(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    windows: np.ndarray,
    valid: np.ndarray,
    policy: EventPolicy,
) -> dict[int, float]:
    first: dict[int, float] = {}
    for window, when in model_event_times(model, ev.subset(valid), windows[valid], policy):
        key = int(window)  # type: ignore[call-overload]
        first[key] = min(when, first.get(key, when))
    return first


def _windows(ev: RowEvaluation, duration: float) -> np.ndarray:
    """Window id per row (non-overlapping ``duration`` s, per piece); -1 unknown."""
    out = np.full(len(ev.seconds), -1, dtype=int)
    next_id = 0
    for piece in pd.unique(ev.pieces):
        idx = np.flatnonzero(ev.pieces == piece)
        secs = ev.seconds[idx]
        finite = np.isfinite(secs)
        if not finite.any():
            continue
        start = np.nanmin(secs)
        local = np.floor((secs - start) / duration)
        local_ids = np.where(finite, local, -1).astype(int)
        uniq = sorted({int(v) for v in local_ids if v >= 0})
        mapping = {v: next_id + k for k, v in enumerate(uniq)}
        next_id += len(uniq)
        out[idx] = [mapping.get(int(v), -1) for v in local_ids]
    return out


def _windows_with_event(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    windows: np.ndarray,
    valid: np.ndarray,
    policy: EventPolicy,
) -> set[int]:
    return set(_first_event_per_window(model, ev, windows, valid, policy))
