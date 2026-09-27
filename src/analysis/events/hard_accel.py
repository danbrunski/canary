"""Hard-acceleration detector.

Triggers when throttle position climbs hard and RPM follows. Useful
for rare-event corpus selection (the IF is usually trained against
nominal idle / cruise, so hard accel windows are good adversarial
examples for replay-based evaluation).
"""

from __future__ import annotations

import pandas as pd

from src.columns import TIMESTAMP_COLUMN
from src.storage.session_products import DetectedEvent, ModeSegment
from src.timefmt import format_timestamp

#: Throttle percent at or above which we call the input "wide open" enough
#: to count as a hard accel — distinct from ``wot_run`` which is a fuller
#: stretch.
_HARD_THROTTLE_PCT = 70.0

#: Minimum RPM rate-of-change (rpm/s) over the window required to confirm
#: the engine actually responded — drops out throttle blips against a closed
#: clutch.
_MIN_RPM_SLOPE = 600.0

#: Minimum duration (s) the throttle must stay above ``_HARD_THROTTLE_PCT``.
_MIN_DURATION_S = 1.0

#: Cooldown (s) between successive hard-accel events so a single sustained
#: stomp counts once, not every sample.
_COOLDOWN_S = 5.0


def detect_hard_accels(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per ``hard-throttle + RPM-rise`` window, with cooldown."""
    del mode_segments
    throttle_col = "obd:THROTTLE_POS"
    rpm_col = "obd:RPM"
    if throttle_col not in segmented.columns or rpm_col not in segmented.columns:
        return []
    if segmented.empty:
        return []
    throttle = pd.to_numeric(segmented[throttle_col], errors="coerce")
    rpm = pd.to_numeric(segmented[rpm_col], errors="coerce")
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)

    is_hard = (throttle >= _HARD_THROTTLE_PCT).fillna(False)
    events: list[DetectedEvent] = []
    last_emit_ts: pd.Timestamp | None = None
    run_start: int | None = None
    n = len(segmented)
    for idx in range(n):
        if is_hard.iloc[idx]:
            if run_start is None:
                run_start = idx
        if (not is_hard.iloc[idx] or idx == n - 1) and run_start is not None:
            run_end = idx if not is_hard.iloc[idx] else idx + 1
            duration = (timestamps.iloc[run_end - 1] - timestamps.iloc[run_start]).total_seconds()
            if duration >= _MIN_DURATION_S:
                rpm_slope = _slope(rpm, timestamps, run_start, run_end)
                if rpm_slope is not None and rpm_slope >= _MIN_RPM_SLOPE:
                    ts_value = timestamps.iloc[run_start]
                    if (
                        last_emit_ts is None
                        or (ts_value - last_emit_ts).total_seconds() >= _COOLDOWN_S
                    ):
                        events.append(
                            DetectedEvent(
                                timestamp=format_timestamp(ts_value),
                                kind="hard_accel",
                                fields={
                                    "peak_throttle_pct": round(
                                        float(throttle.iloc[run_start:run_end].max()), 1
                                    ),
                                    "duration_seconds": round(float(duration), 2),
                                    "rpm_slope_per_s": round(float(rpm_slope), 1),
                                },
                            )
                        )
                        last_emit_ts = ts_value
            run_start = None
    return events


def _slope(values: pd.Series, timestamps: pd.Series, start_idx: int, end_idx: int) -> float | None:
    if end_idx - start_idx < 2:
        return None
    y0 = values.iloc[start_idx]
    y1 = values.iloc[end_idx - 1]
    if pd.isna(y0) or pd.isna(y1):
        return None
    dt = (timestamps.iloc[end_idx - 1] - timestamps.iloc[start_idx]).total_seconds()
    if dt <= 0:
        return None
    return float((y1 - y0) / dt)
