"""Cold-start detector.

A cold start: the engine was off long enough for the coolant to fall
near ambient, and the first 30-90 seconds of run-time exhibit the
characteristic warmup curve (coolant rising while RPM holds idle).
Useful for refit corpus selection — the ``coolant_warmup`` behavior
surface must be trained against actual cold starts, not warm
restarts where coolant is already at thermostat target.
"""

from __future__ import annotations

import pandas as pd

from src.analysis.session_classifier import DetectedEvent, ModeSegment
from src.preprocessing.resampler import TIMESTAMP_COLUMN
from src.timefmt import format_timestamp

#: Coolant temperature (°C) below which the engine is considered cold.
_COLD_COOLANT_C = 50.0

#: Engine run-time (s) below which we'll still consider a row part of the
#: starting transient. Run-time resets at each ignition cycle, so the
#: first sample post-start sits near 0.
_FRESH_RUN_TIME_S = 30.0


def detect_cold_starts(
    segmented: pd.DataFrame,
    mode_segments: list[ModeSegment],
) -> list[DetectedEvent]:
    """One event per cold-start transient at the head of a session.

    Conservative: only emits when both coolant and run-time say "just
    started" simultaneously. A warm restart (coolant already 85 °C even
    though run-time = 5 s) doesn't trigger.
    """
    del mode_segments  # consulted for future cross-session correlation; unused today
    coolant_col = "obd:COOLANT_TEMP"
    runtime_col = "obd:RUN_TIME"
    if coolant_col not in segmented.columns or runtime_col not in segmented.columns:
        return []
    if segmented.empty:
        return []

    coolant = pd.to_numeric(segmented[coolant_col], errors="coerce")
    runtime = pd.to_numeric(segmented[runtime_col], errors="coerce")
    mask = (coolant <= _COLD_COOLANT_C) & (runtime <= _FRESH_RUN_TIME_S)
    if not mask.any():
        return []

    first_idx = int(mask.idxmax())
    first_row = segmented.iloc[first_idx]
    return [
        DetectedEvent(
            timestamp=format_timestamp(first_row[TIMESTAMP_COLUMN]),
            kind="cold_start",
            fields={
                "coolant_temp_c": _maybe_float(first_row.get(coolant_col)),
                "run_time_s": _maybe_float(first_row.get(runtime_col)),
            },
        )
    ]


def _maybe_float(value: object) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
