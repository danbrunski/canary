"""Hard-brake detector.

Triggers when speed drops sharply over a short window. Deliberately
inferred from speed alone (not brake-pedal CAN) so it works on any
session that captures ``obd:SPEED`` — brake-pedal CAN signals are
DBC-dependent and not universally available.
"""

from __future__ import annotations

import pandas as pd

from src.analysis.session_classifier import DetectedEvent, ModeSegment
from src.columns import TIMESTAMP_COLUMN
from src.timefmt import format_timestamp

#: Deceleration (kph/s) below which we count the slowdown as "hard".
#: 1 g ≈ 35 kph/s — so 15 kph/s is a brisk panic-stop without quite being
#: an emergency. Tuned to be selective against rough roads and downshifts.
_MIN_DECEL_KPH_PER_S = -15.0

#: Window length (s) over which the deceleration is averaged. Larger
#: values smooth out sensor jitter; smaller catches sharper events.
_WINDOW_SECONDS = 2.0

#: Cooldown between successive emitted hard-brake events.
_COOLDOWN_S = 5.0


def detect_hard_brakes(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per ``Δspeed/Δt ≤ threshold`` window, with cooldown."""
    del mode_segments
    speed_col = "obd:SPEED"
    if speed_col not in segmented.columns or segmented.empty:
        return []
    speed = pd.to_numeric(segmented[speed_col], errors="coerce")
    timestamps = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    events: list[DetectedEvent] = []
    n = len(segmented)
    last_emit_ts: pd.Timestamp | None = None
    window = pd.Timedelta(seconds=_WINDOW_SECONDS)
    for idx in range(n):
        end_ts = timestamps.iloc[idx]
        start_ts = end_ts - window
        # Find the earliest sample within the window.
        start_idx = int(timestamps.searchsorted(start_ts, side="left"))
        if start_idx >= idx:
            continue
        v0 = speed.iloc[start_idx]
        v1 = speed.iloc[idx]
        if pd.isna(v0) or pd.isna(v1):
            continue
        dt = (end_ts - timestamps.iloc[start_idx]).total_seconds()
        if dt <= 0:
            continue
        slope = float((v1 - v0) / dt)
        if slope > _MIN_DECEL_KPH_PER_S:
            continue
        if last_emit_ts is not None and (end_ts - last_emit_ts).total_seconds() < _COOLDOWN_S:
            continue
        events.append(
            DetectedEvent(
                timestamp=format_timestamp(end_ts),
                kind="hard_brake",
                fields={
                    "decel_kph_per_s": round(slope, 2),
                    "speed_from_kph": round(float(v0), 1),
                    "speed_to_kph": round(float(v1), 1),
                },
            )
        )
        last_emit_ts = end_ts
    return events
