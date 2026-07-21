"""Pre-resample decimation for high-rate IMU channels (Phase 2a.3).

See ``docs/roadmap/imu-gps-integration.md`` ("Decimation into the wide
frame"): a 100 Hz IMU must not flow raw into the feature pipeline the way
1-5 Hz OBD does — every distinct sample becomes its own row in ``to_wide``'s
per-timestamp pivot, dwarfing the OBD rows and bloating the pivot the
resampler then has to collapse.

This module runs on the WIDE frame — after ``loader.to_wide`` pivots long
telemetry to one column per ``source:channel``, before ``resampler.
resample_wide`` collapses it onto the uniform tick grid. Raw per-sample rows
still land in Parquet unchanged (the storage layer never sees this module);
only the in-memory wide frame that feeds the resampler is decimated, and
only for the specific longitudinal/lateral channels the auto-tagger and
segmenter actually consume: ``imu:ACCEL_LON_MAX/RMS``, ``imu:JERK_MAX``,
``imu:ACCEL_LAT_MAX``.

Why MAX/RMS instead of plain resampling: ``resample_wide`` uses
``.resample(interval).last()`` — appropriate for slow, roughly-continuous
OBD signals, but for a 100 Hz accelerometer it would silently keep one
arbitrary raw sample per tick and discard every transient peak in between
(exactly the signal a hard-brake/hard-accel tag needs). Aggregating to one
row-broadcast value per resample-tick bin *before* ``resample_wide`` runs
means its own ``.last()`` picks up the already-correct aggregate instead of
an arbitrary raw sample.

A no-op (returns the input unchanged) whenever neither longitudinal nor
lateral IMU channel is present — the case for every session recorded before
Phase 2b hardware exists.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.telemetry import channel_key

IMU_SOURCE = "imu"

#: Frame-resolved (post ``imu_frame.py``) longitudinal/lateral linear-accel
#: input channels this step decimates. Column presence is all this module
#: cares about — it's equally safe to run over raw body-frame ACCEL_LON/LAT
#: (e.g. in a unit test that skips frame resolution), since the aggregation
#: math doesn't depend on how the values got there.
ACCEL_LON_COLUMN = channel_key(IMU_SOURCE, "ACCEL_LON")
ACCEL_LAT_COLUMN = channel_key(IMU_SOURCE, "ACCEL_LAT")

#: Decimated output columns (Phase 2a.3's exact list from the roadmap doc).
ACCEL_LON_MAX_COLUMN = channel_key(IMU_SOURCE, "ACCEL_LON_MAX")
ACCEL_LON_RMS_COLUMN = channel_key(IMU_SOURCE, "ACCEL_LON_RMS")
ACCEL_LAT_MAX_COLUMN = channel_key(IMU_SOURCE, "ACCEL_LAT_MAX")
JERK_MAX_COLUMN = channel_key(IMU_SOURCE, "JERK_MAX")


def decimate_imu_wide(wide: pd.DataFrame, *, hz: float) -> pd.DataFrame:
    """Collapse raw-rate ``imu:ACCEL_LON``/``imu:ACCEL_LAT`` to per-tick aggregates.

    Rows are bucketed by ``(session, floor(timestamp, 1/hz))`` so a tick's
    aggregate never mixes samples across a session boundary or an adjacent
    tick. The aggregate is broadcast back onto every raw row in its bucket
    (not collapsed to one row) so ``resample_wide``'s own
    ``.resample(interval).last()`` — run immediately after this by the
    caller — lands on the already-decimated value regardless of which raw
    timestamp happens to be "last" in that interval.

    The raw ``imu:ACCEL_LON``/``imu:ACCEL_LAT`` columns are dropped from the
    output once decimated (only the aggregates are meant to reach the
    feature frame); every other column, including other ``imu:*`` channels,
    passes through untouched.
    """
    present = [c for c in (ACCEL_LON_COLUMN, ACCEL_LAT_COLUMN) if c in wide.columns]
    if wide.empty or not present:
        return wide
    if hz <= 0:
        raise ValueError("hz must be positive")

    out = wide.copy()
    tick = pd.Timedelta(seconds=1.0 / hz)
    timestamps = pd.to_datetime(out[TIMESTAMP_COLUMN], utc=True)
    bin_start = timestamps.dt.floor(tick)  # type: ignore[arg-type]
    session = (
        out[SESSION_COLUMN].astype(str)
        if SESSION_COLUMN in out.columns
        else pd.Series("", index=out.index)
    )
    # A tuple-valued grouping key (session, tick bucket) — pandas groupby
    # accepts an object-dtype Series of hashables directly, one group per
    # distinct (session, tick) pair.
    group = pd.Series(list(zip(session, bin_start, strict=True)), index=out.index)

    if ACCEL_LON_COLUMN in out.columns:
        lon = pd.to_numeric(out[ACCEL_LON_COLUMN], errors="coerce")
        grouped_lon = lon.groupby(group)
        out[ACCEL_LON_MAX_COLUMN] = grouped_lon.transform(lambda s: s.abs().max())
        out[ACCEL_LON_RMS_COLUMN] = grouped_lon.transform(lambda s: np.sqrt((s**2).mean()))
        # Jerk = d(accel)/dt over consecutive raw samples *within* a tick
        # bucket — ``groupby(...).diff()`` resets at each group's first row
        # (NaN there), so it never differences across a bucket boundary.
        dt = timestamps.groupby(group).diff().dt.total_seconds()  # type: ignore[attr-defined]
        jerk = (lon.groupby(group).diff() / dt.replace(0.0, np.nan)).abs()
        out[JERK_MAX_COLUMN] = jerk.groupby(group).transform("max")
        out = out.drop(columns=[ACCEL_LON_COLUMN])

    if ACCEL_LAT_COLUMN in out.columns:
        lat = pd.to_numeric(out[ACCEL_LAT_COLUMN], errors="coerce")
        out[ACCEL_LAT_MAX_COLUMN] = lat.groupby(group).transform(lambda s: s.abs().max())
        out = out.drop(columns=[ACCEL_LAT_COLUMN])

    return out
