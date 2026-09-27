"""Cruise detector — sustained near-constant speed.

Either city or highway cruise depending on the speed band. Useful as
clean baseline data for IF/LSTM training, and as a corpus marker
("here are 4 minutes of 65 mph cruise"). Distinct from
``idle_period`` (zero speed) and from ``hard_accel`` (transient).
"""

from __future__ import annotations

import pandas as pd

from src.analysis.session_classifier import DetectedEvent, ModeSegment
from src.columns import MODE_COLUMN, TIMESTAMP_COLUMN
from src.operating import HIGHWAY_MODE
from src.timefmt import format_timestamp

#: Speed below which we don't bother flagging cruise — that's idle / stop-and-go.
_MIN_CRUISE_SPEED_KPH = 25.0

#: Allowed band (kph) around the median speed of the window. ±5 keeps a
#: 60 kph cruise from being broken up by a momentary 65, but a swing from
#: 40 to 60 splits into two segments.
_BAND_KPH = 5.0

#: Minimum sustained duration (s) for a cruise window to count.
_MIN_DURATION_S = 30.0


def _regime(segmented: pd.DataFrame, start: int, stop: int) -> str:
    """``highway`` when the segmenter labelled most of the window highway.

    Reads the segmenter's own band instead of restating it (R01.7): this said
    >= 70 km/h while the segmenter's highway band starts at 80, so a 72 km/h
    cruise was a highway cruise here and a city row everywhere else.
    """
    if MODE_COLUMN not in segmented.columns:
        return "surface"
    modes = segmented[MODE_COLUMN].iloc[start:stop]
    return "highway" if bool((modes == HIGHWAY_MODE).mean() >= 0.5) else "surface"


def detect_cruise(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """Greedy walk: extend the current cruise window while speed stays in
    band; close it when the band breaks or we go below the cruise floor.
    """
    del mode_segments
    speed_col = "obd:SPEED"
    if speed_col not in segmented.columns or segmented.empty:
        return []
    speed = pd.to_numeric(segmented[speed_col], errors="coerce")
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    events: list[DetectedEvent] = []
    n = len(segmented)
    idx = 0
    while idx < n:
        if pd.isna(speed.iloc[idx]) or speed.iloc[idx] < _MIN_CRUISE_SPEED_KPH:
            idx += 1
            continue
        run_start = idx
        center = speed.iloc[idx]
        end_idx = idx + 1
        while end_idx < n:
            value = speed.iloc[end_idx]
            if pd.isna(value):
                break
            if value < _MIN_CRUISE_SPEED_KPH or abs(value - center) > _BAND_KPH:
                break
            end_idx += 1
        duration = (timestamps.iloc[end_idx - 1] - timestamps.iloc[run_start]).total_seconds()
        if duration >= _MIN_DURATION_S:
            mean_speed = float(speed.iloc[run_start:end_idx].mean())
            events.append(
                DetectedEvent(
                    timestamp=format_timestamp(timestamps.iloc[run_start]),
                    kind="cruise",
                    fields={
                        "mean_speed_kph": round(mean_speed, 1),
                        "duration_seconds": round(float(duration), 2),
                        "regime": _regime(segmented, run_start, end_idx),
                    },
                )
            )
        idx = end_idx
    return events
