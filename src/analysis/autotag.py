"""Motion-aware auto-tagger: the shared taxonomy for Phase 2a.5/2a.6.

Both ``scripts/autotag.py`` (offline, a batch pass over a session's
resampled/segmented frame) and the daemon's live hook (Phase 2a.6 — ships
config-gated off, see ``AutoTagConfig.live_enabled``) write the exact same
tag records the manual ``scripts/tag.py`` writes
(``src.collection.tags.write_tag``), from the exact same threshold table
(``docs/roadmap/imu-gps-integration.md`` "The auto-tagger"):

| Label             | Trigger                                                    | Sensors  |
|-------------------|-------------------------------------------------------------|----------|
| ``hard_accel``    | ``imu:ACCEL_LON_MAX`` > θ_a and d(``obd:SPEED``)/dt > 0     | IMU+OBD  |
| ``hard_brake``    | ``imu:ACCEL_LON_MAX`` > θ_b and d(``obd:SPEED``)/dt < 0     | IMU+OBD  |
| ``cornering``     | ``imu:ACCEL_LAT_MAX`` > θ_c                                  | IMU      |
| ``launch``        | ``obd:SPEED`` ≈ 0 → sustained ``hard_accel``                | IMU+OBD  |
| ``stopped``       | ``gps:speed`` ≈ 0 (bus-independent)                          | GPS      |
| ``highway_cruise``| ``gps:speed`` > θ_h sustained, low lat/lon accel variance    | GPS+IMU  |

This module is the pure detection layer: it never touches the filesystem.
``detect_tags`` takes a resampled/segmented session frame and a config and
returns a list of :class:`TagEvent`; the offline script and the live hook
each decide *when* to call it and how to turn the result into a
``write_tag`` call (idempotency, live edge-triggering, etc. live there).

The GPS-only detectors (``stopped``, ``highway_cruise``) work against any
session with ``gps:speed`` — real stored sessions today. The IMU-dependent
detectors (``hard_accel``/``hard_brake``/``cornering``/``launch``) are
correctly-implemented and unit-tested against synthetic data, but are
dormant/no-op against every real session recorded before Phase 2b hardware
exists — there is no ``imu:*`` column to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.config import AutoTagConfig

TIMESTAMP_COLUMN = "timestamp"


@dataclass(frozen=True)
class TagEvent:
    """One candidate auto-tag, ready to hand to ``src.collection.tags.write_tag``."""

    timestamp: pd.Timestamp
    label: str
    fields: dict[str, Any] = field(default_factory=dict)


def _infer_median_dt_seconds(timestamps: pd.Series) -> float:
    diffs = pd.to_datetime(timestamps, utc=True).diff().dt.total_seconds().dropna()
    if diffs.empty:
        return 1.0
    median = float(diffs.median())
    return median if median > 0 else 1.0


def _contiguous_runs(condition: pd.Series) -> list[tuple[int, int]]:
    """Return ``(start_pos, end_pos)`` (inclusive, positional) for each
    maximal run of consecutive ``True`` values in ``condition``."""
    values = condition.fillna(False).to_numpy()
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate(values):
        if value and start is None:
            start = i
        elif not value and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(values) - 1))
    return runs


def _peak_events(
    frame: pd.DataFrame,
    condition: pd.Series,
    metric: pd.Series,
    label: str,
    *,
    field_name: str,
) -> list[TagEvent]:
    """One event per contiguous ``condition`` run, at that run's peak |metric| row."""
    events: list[TagEvent] = []
    metric_values = pd.to_numeric(metric, errors="coerce")
    for start, end in _contiguous_runs(condition):
        window = metric_values.iloc[start : end + 1]
        if window.isna().all():
            continue
        peak_pos = start + int(window.abs().to_numpy().argmax())
        events.append(
            TagEvent(
                timestamp=pd.Timestamp(frame[TIMESTAMP_COLUMN].iloc[peak_pos]),
                label=label,
                fields={field_name: float(metric_values.iloc[peak_pos])},
            )
        )
    return events


def _onset_events(
    frame: pd.DataFrame,
    condition: pd.Series,
    metric: pd.Series,
    label: str,
    *,
    field_name: str,
    min_run_rows: int,
) -> list[TagEvent]:
    """One event per contiguous ``condition`` run that lasts >= ``min_run_rows``,
    at that run's *first* row (the moment the condition started holding)."""
    events: list[TagEvent] = []
    metric_values = pd.to_numeric(metric, errors="coerce")
    for start, end in _contiguous_runs(condition):
        if end - start + 1 < min_run_rows:
            continue
        events.append(
            TagEvent(
                timestamp=pd.Timestamp(frame[TIMESTAMP_COLUMN].iloc[start]),
                label=label,
                fields={field_name: float(metric_values.iloc[start])},
            )
        )
    return events


def _speed_derivative_sign(frame: pd.DataFrame) -> pd.Series | None:
    if "obd:SPEED" not in frame.columns:
        return None
    speed = pd.to_numeric(frame["obd:SPEED"], errors="coerce")
    dt = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True).diff().dt.total_seconds()
    return speed.diff() / dt.replace(0.0, np.nan)


def detect_hard_accel_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """IMU + OBD — no GPS needed (see the doc's "physics, settled" section)."""
    if "imu:ACCEL_LON_MAX" not in frame.columns:
        return []
    speed_accel = _speed_derivative_sign(frame)
    if speed_accel is None:
        return []
    accel = pd.to_numeric(frame["imu:ACCEL_LON_MAX"], errors="coerce")
    condition = (accel > config.hard_accel_g) & (speed_accel > 0)
    return _peak_events(frame, condition, accel, "hard_accel", field_name="peak_g")


def detect_hard_brake_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    if "imu:ACCEL_LON_MAX" not in frame.columns:
        return []
    speed_accel = _speed_derivative_sign(frame)
    if speed_accel is None:
        return []
    accel = pd.to_numeric(frame["imu:ACCEL_LON_MAX"], errors="coerce")
    condition = (accel > config.hard_brake_g) & (speed_accel < 0)
    return _peak_events(frame, condition, accel, "hard_brake", field_name="peak_g")


def detect_cornering_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """IMU only (heading-resolved lateral accel) — no sign disambiguation yet
    (doc's documented risk: bleeds into hard_accel until yaw offset converges)."""
    if "imu:ACCEL_LAT_MAX" not in frame.columns:
        return []
    accel = pd.to_numeric(frame["imu:ACCEL_LAT_MAX"], errors="coerce")
    condition = accel > config.cornering_g
    return _peak_events(frame, condition, accel, "cornering", field_name="peak_g")


def detect_launch_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """``obd:SPEED`` ≈ 0 → sustained hard_accel (IMU + OBD)."""
    if "imu:ACCEL_LON_MAX" not in frame.columns or "obd:SPEED" not in frame.columns:
        return []
    speed_accel = _speed_derivative_sign(frame)
    if speed_accel is None:
        return []
    accel = pd.to_numeric(frame["imu:ACCEL_LON_MAX"], errors="coerce")
    speed = pd.to_numeric(frame["obd:SPEED"], errors="coerce")
    condition = (
        (speed <= config.launch_speed_max_kph) & (accel > config.hard_accel_g) & (speed_accel > 0)
    )
    dt = _infer_median_dt_seconds(frame[TIMESTAMP_COLUMN])
    min_run_rows = max(1, round(config.launch_sustain_seconds / dt))
    return _onset_events(
        frame, condition, accel, "launch", field_name="peak_g", min_run_rows=min_run_rows
    )


def detect_stopped_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """GPS only — bus-independent, works even with no OBD/CAN fix at all."""
    if "gps:speed" not in frame.columns:
        return []
    speed = pd.to_numeric(frame["gps:speed"], errors="coerce")
    condition = speed <= config.stopped_speed_max_kph
    return _onset_events(
        frame, condition, speed, "stopped", field_name="gps_speed_kph", min_run_rows=1
    )


def detect_highway_cruise_events(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """GPS speed sustained above threshold; IMU accel variance gate applied
    only when IMU columns are present (never blocks the GPS-only case)."""
    if "gps:speed" not in frame.columns:
        return []
    speed = pd.to_numeric(frame["gps:speed"], errors="coerce")
    condition = speed >= config.highway_speed_min_kph

    has_lon = "imu:ACCEL_LON_MAX" in frame.columns
    has_lat = "imu:ACCEL_LAT_MAX" in frame.columns
    if has_lon or has_lat:
        window = max(
            2,
            round(
                config.highway_sustain_seconds / _infer_median_dt_seconds(frame[TIMESTAMP_COLUMN])
            ),
        )
        variance = pd.Series(0.0, index=frame.index)
        if has_lon:
            variance = variance.add(
                pd.to_numeric(frame["imu:ACCEL_LON_MAX"], errors="coerce")
                .rolling(window, min_periods=1)
                .var()
                .fillna(0.0),
                fill_value=0.0,
            )
        if has_lat:
            variance = variance.add(
                pd.to_numeric(frame["imu:ACCEL_LAT_MAX"], errors="coerce")
                .rolling(window, min_periods=1)
                .var()
                .fillna(0.0),
                fill_value=0.0,
            )
        condition = condition & (variance <= config.highway_max_accel_variance_g2)

    dt = _infer_median_dt_seconds(frame[TIMESTAMP_COLUMN])
    min_run_rows = max(1, round(config.highway_sustain_seconds / dt))
    return _onset_events(
        frame,
        condition,
        speed,
        "highway_cruise",
        field_name="gps_speed_kph",
        min_run_rows=min_run_rows,
    )


#: Every detector, in the order the doc's table lists them. Both the offline
#: script and the live hook iterate this rather than hand-listing detectors,
#: so a new tag added here reaches both call sites for free.
DETECTORS = (
    detect_hard_accel_events,
    detect_hard_brake_events,
    detect_cornering_events,
    detect_launch_events,
    detect_stopped_events,
    detect_highway_cruise_events,
)


def detect_tags(frame: pd.DataFrame, config: AutoTagConfig) -> list[TagEvent]:
    """Run every detector over ``frame`` and return all events, time-ordered."""
    events: list[TagEvent] = []
    for detector in DETECTORS:
        events.extend(detector(frame, config))
    events.sort(key=lambda event: event.timestamp)
    return events
