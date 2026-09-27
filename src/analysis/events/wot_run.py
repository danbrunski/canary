"""Wide-open-throttle detector.

Sibling to ``hard_accel``: that detector picks the transient
"floor it from a stop" event; this one picks the sustained ≥ 90%
throttle stretch (passing run on the highway, on-ramp merge). Both
are useful corpus markers, especially for evaluating IF / LSTM on
high-load operating points.
"""

from __future__ import annotations

import pandas as pd

from src.analysis.session_classifier import DetectedEvent, ModeSegment
from src.columns import TIMESTAMP_COLUMN
from src.timefmt import format_timestamp

#: Throttle percent at or above which we call the input "wide open" for this
#: detector. Distinct from ``hard_accel``'s 70 % — this is the higher bar.
_WOT_THROTTLE_PCT = 90.0

#: Minimum sustained duration (s).
_MIN_DURATION_S = 2.0

#: Cooldown between successive emitted events.
_COOLDOWN_S = 5.0


def detect_wot_runs(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per sustained ``throttle ≥ _WOT_THROTTLE_PCT`` run."""
    del mode_segments
    throttle_col = "obd:THROTTLE_POS"
    if throttle_col not in segmented.columns or segmented.empty:
        return []
    throttle = pd.to_numeric(segmented[throttle_col], errors="coerce")
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    is_wot = (throttle >= _WOT_THROTTLE_PCT).fillna(False)
    events: list[DetectedEvent] = []
    last_emit_ts: pd.Timestamp | None = None
    run_start: int | None = None
    n = len(segmented)
    for idx in range(n):
        if is_wot.iloc[idx]:
            if run_start is None:
                run_start = idx
            continue
        if run_start is not None:
            _maybe_emit(events, run_start, idx, throttle, timestamps, last_emit_ts)
            if events and events[-1].timestamp:
                last_emit_ts = pd.to_datetime(events[-1].timestamp, utc=True)
            run_start = None
    if run_start is not None:
        _maybe_emit(events, run_start, n, throttle, timestamps, last_emit_ts)
    return events


def _maybe_emit(
    events: list[DetectedEvent],
    run_start: int,
    run_end: int,
    throttle: pd.Series,
    timestamps: pd.Series,
    last_emit_ts: pd.Timestamp | None,
) -> None:
    duration = (timestamps.iloc[run_end - 1] - timestamps.iloc[run_start]).total_seconds()
    if duration < _MIN_DURATION_S:
        return
    start_ts = timestamps.iloc[run_start]
    if last_emit_ts is not None and (start_ts - last_emit_ts).total_seconds() < _COOLDOWN_S:
        return
    events.append(
        DetectedEvent(
            timestamp=format_timestamp(start_ts),
            kind="wot_run",
            fields={
                "peak_throttle_pct": round(float(throttle.iloc[run_start:run_end].max()), 1),
                "duration_seconds": round(float(duration), 2),
            },
        )
    )
