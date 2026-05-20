"""Stop detector — speed transitions from rolling to zero.

Counts stoplights / stop signs / parking arrivals. A stop is the
*event* of decelerating to zero; the subsequent dwell is an
``idle_period``. Useful drive-style indicator and good corpus marker
for braking-related faults.
"""

from __future__ import annotations

import pandas as pd

from src.analysis.session_classifier import DetectedEvent, ModeSegment
from src.preprocessing.resampler import TIMESTAMP_COLUMN
from src.timefmt import format_timestamp

#: Speed threshold below which we call the vehicle "stopped" (kph).
_STOPPED_SPEED_KPH = 1.0

#: Minimum prior speed (kph) so we don't fire on every brief drift to zero
#: in stop-and-go. Operator wants meaningful stops, not vibration.
_MIN_PRE_STOP_SPEED_KPH = 8.0

#: Minimum time (s) the vehicle must hold zero speed for the transition to
#: register — drops out a one-tick zero-crossing during rough deceleration.
_MIN_STOP_DURATION_S = 2.0


def detect_stops(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per ``rolling → stopped → held`` transition."""
    del mode_segments
    speed_col = "obd:SPEED"
    if speed_col not in segmented.columns or segmented.empty:
        return []
    speed = pd.to_numeric(segmented[speed_col], errors="coerce").ffill()
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    is_stopped = (speed <= _STOPPED_SPEED_KPH).fillna(False)

    events: list[DetectedEvent] = []
    n = len(segmented)
    idx = 0
    while idx < n:
        # Find the next stopped-stretch boundary.
        if not is_stopped.iloc[idx]:
            idx += 1
            continue
        # Pre-stop speed: the most recent above-threshold speed value we saw.
        # Skip the case where the session opens stopped (no prior motion).
        pre_speed = _last_rolling_speed(speed, idx)
        if pre_speed is None or pre_speed < _MIN_PRE_STOP_SPEED_KPH:
            idx = _advance_past_run(is_stopped, idx)
            continue
        run_end = _advance_past_run(is_stopped, idx)
        duration = (timestamps.iloc[run_end - 1] - timestamps.iloc[idx]).total_seconds()
        if duration >= _MIN_STOP_DURATION_S:
            events.append(
                DetectedEvent(
                    timestamp=format_timestamp(timestamps.iloc[idx]),
                    kind="stop",
                    fields={
                        "pre_stop_speed_kph": round(float(pre_speed), 2),
                        "stopped_duration_seconds": round(float(duration), 2),
                    },
                )
            )
        idx = run_end
    return events


def _last_rolling_speed(speed: pd.Series, start_idx: int) -> float | None:
    """Most recent above-threshold speed in the window leading up to start_idx."""
    for i in range(start_idx - 1, -1, -1):
        value = speed.iloc[i]
        if pd.notna(value) and value > _MIN_PRE_STOP_SPEED_KPH:
            return float(value)
    return None


def _advance_past_run(is_stopped: pd.Series, start_idx: int) -> int:
    idx = start_idx
    while idx < len(is_stopped) and bool(is_stopped.iloc[idx]):
        idx += 1
    return idx
