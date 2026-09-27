"""Idle-period detector — extended stretches at zero speed.

Distinct from the ``stop`` detector: a stop is the *transition* to
zero speed; an idle_period is a *sustained* run of it. Useful for
characterising drive style ("you spent 12 minutes idling") and for
gating per-mode model training that needs continuous idle data.
"""

from __future__ import annotations

import pandas as pd

from src.columns import TIMESTAMP_COLUMN
from src.storage.session_products import DetectedEvent, ModeSegment
from src.timefmt import format_timestamp

#: Speed below which we treat the vehicle as idling.
_IDLE_SPEED_KPH = 1.0

#: Minimum continuous duration for an idle stretch to register as an event.
_MIN_DURATION_S = 10.0


def detect_idle_periods(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per contiguous idle stretch ≥ ``_MIN_DURATION_S``."""
    del mode_segments
    speed_col = "obd:SPEED"
    if speed_col not in segmented.columns or segmented.empty:
        return []
    speed = pd.to_numeric(segmented[speed_col], errors="coerce")
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    is_idle = (speed <= _IDLE_SPEED_KPH).fillna(False)

    events: list[DetectedEvent] = []
    run_start: int | None = None
    for idx in range(len(segmented)):
        if is_idle.iloc[idx]:
            if run_start is None:
                run_start = idx
        else:
            if run_start is not None:
                events.extend(_emit(timestamps, run_start, idx))
                run_start = None
    if run_start is not None:
        events.extend(_emit(timestamps, run_start, len(segmented)))
    return events


def _emit(timestamps: pd.Series, start_idx: int, end_idx: int) -> list[DetectedEvent]:
    start_ts = timestamps.iloc[start_idx]
    end_ts = timestamps.iloc[end_idx - 1]
    duration = (end_ts - start_ts).total_seconds()
    if duration < _MIN_DURATION_S:
        return []
    return [
        DetectedEvent(
            timestamp=format_timestamp(start_ts),
            kind="idle_period",
            fields={
                "duration_seconds": round(float(duration), 2),
                "end_timestamp": format_timestamp(end_ts),
            },
        )
    ]
