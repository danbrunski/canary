"""Parquet session loading utilities for preprocessing."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.preprocessing.imu_decimation import (
    ACCEL_LAT_COLUMN,
    ACCEL_LAT_MAX_COLUMN,
    ACCEL_LON_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_RMS_COLUMN,
    JERK_MAX_COLUMN,
    decimate_imu_wide,
)
from src.preprocessing.resampler import (
    GAP_SUFFIX,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    ResampleConfig,
    resample_long,
    resample_wide,
)
from src.preprocessing.segmenter import MODE_COLUMN, segment_operating_modes
from src.telemetry import SOURCE_SEP
from src.telemetry import channel_key as channel_key

log = logging.getLogger(__name__)

PARQUET_PATTERN = "telemetry-*.parquet"
INDEX_COLUMNS = ["session", "timestamp"]
TelemetryValueKind = Literal["value", "value_number", "value_text", "value_bool", "payload"]


def discover_sessions(root: Path) -> list[Path]:
    """Return session directories under root that contain telemetry Parquet chunks.

    Directories whose name starts with ``_`` are skipped, so quarantined /
    schema-incomplete sessions can be moved under ``<root>/_archive/`` and
    excluded from every training run without touching the data.
    """
    if not root.exists():
        return []
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith("_")
        and any(f.stat().st_size > 0 for f in child.glob(PARQUET_PATTERN))
    )


def count_session_rows(session_dir: Path) -> int | None:
    """Row count of a session's Parquet chunks from footer metadata only.

    No data read, so this is cheap enough to call before a replay starts
    (D02.9 reporting) or while indexing a session. ``None`` means "unknown"
    (nothing readable) as distinct from a genuine zero.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency elsewhere
        return None
    total = 0
    seen = False
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            if chunk.stat().st_size == 0:
                continue
            # pyarrow's typing of ParquetFile.__init__ varies by version (untyped
            # in some, annotated in others) — unused-ignore keeps this line green
            # under both, since CI and local envs can resolve different pyarrow.
            total += pq.ParquetFile(chunk).metadata.num_rows  # type: ignore[no-untyped-call, unused-ignore]
            seen = True
        except Exception:  # noqa: BLE001 - a corrupt/truncated chunk just doesn't count
            continue
    return total if seen else None


def read_session(session_dir: Path) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame."""
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")

    # A daemon crash mid-write leaves trailing 0-byte or truncated chunks
    # (the atomic writer's target appears before its content lands). One bad
    # tail chunk must not poison the whole training corpus — skip the
    # unreadable ones and keep going.
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    for path in candidates:
        if path.stat().st_size == 0:
            skipped.append(path.name)
            continue
        try:
            frames.append(pd.read_parquet(path))
        except (OSError, ValueError, pd.errors.ParserError):
            skipped.append(path.name)
    if skipped:
        log.warning(
            "skipped %d unreadable chunk(s) in %s: %s",
            len(skipped),
            session_dir.name,
            ", ".join(skipped),
        )
    if not frames:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["session"] = session_dir.name
    frame["session_path"] = str(session_dir)
    frame["source_channel"] = (
        frame["source"].astype(str) + SOURCE_SEP + frame["channel"].astype(str)
    )
    frame["value"] = _coalesce_value_columns(frame)
    frame = frame.sort_values(["timestamp", "source", "channel"], kind="stable")
    return frame.reset_index(drop=True)


def read_sessions(session_dirs: Iterable[Path]) -> pd.DataFrame:
    """Read several session directories into one long-format DataFrame."""
    frames = [read_session(path) for path in session_dirs]
    if not frames:
        return _empty_long_frame()
    return pd.concat(frames, ignore_index=True).sort_values(
        ["session", "timestamp", "source", "channel"],
        kind="stable",
    )


def _read_session_numeric(session_dir: Path) -> pd.DataFrame:
    """Read only columns needed by the numeric batch resampler."""
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    frames: list[pd.DataFrame] = []
    for path in candidates:
        if path.stat().st_size == 0:
            continue
        try:
            frames.append(
                pd.read_parquet(path, columns=["timestamp", "source", "channel", "value_number"])
            )
        except (OSError, ValueError, pd.errors.ParserError):
            continue
    if not frames:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    frame = pd.concat(frames, ignore_index=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["session"] = session_dir.name
    frame["source_channel"] = (
        frame["source"].astype(str) + SOURCE_SEP + frame["channel"].astype(str)
    )
    return frame[["session", "timestamp", "source_channel", "value_number"]]


def _decimate_imu_long(frame: pd.DataFrame, *, hz: float) -> pd.DataFrame:
    """Long-form equivalent of :func:`decimate_imu_wide` without a raw pivot."""
    raw_channels = {ACCEL_LON_COLUMN, ACCEL_LAT_COLUMN}
    if frame.empty or not bool(frame["source_channel"].isin(raw_channels).any()):
        return frame
    tick = pd.Timedelta(seconds=1.0 / hz)
    work = frame.copy()
    work["_tick"] = work["timestamp"].dt.floor(tick)
    # Wide decimation broadcasts each aggregate over every raw timestamp in
    # its tick; resample_wide then observes it at that tick's final timestamp.
    grouped_timestamps = work.groupby("_tick", sort=True)["timestamp"]
    observed_first = grouped_timestamps.min()
    observed_last = grouped_timestamps.max()
    synthetic: list[pd.DataFrame] = []

    def append_metric(values: pd.Series, channel: str) -> None:
        starts = observed_first.reindex(values.index)
        ends = observed_last.reindex(values.index)
        # Two observations reproduce the wide path's broadcast without one
        # synthetic row per raw sample: the first controls whether the value
        # exists at this grid edge, and the last controls age at the next.
        timestamps = pd.concat([starts, ends]).drop_duplicates()
        repeated = values.reindex(timestamps.index)
        synthetic.append(
            pd.DataFrame(
                {
                    "session": frame["session"].iloc[0],
                    "timestamp": timestamps.to_numpy(),
                    "source_channel": channel,
                    "value_number": repeated.to_numpy(),
                }
            )
        )

    lon = work.loc[work["source_channel"] == ACCEL_LON_COLUMN].copy()
    if not lon.empty:
        lon = lon.sort_values("timestamp", kind="stable").drop_duplicates("timestamp", keep="last")
        values = pd.to_numeric(lon["value_number"], errors="coerce")
        grouped = values.groupby(lon["_tick"])
        append_metric(grouped.apply(lambda series: series.abs().max()), ACCEL_LON_MAX_COLUMN)
        append_metric(
            grouped.apply(lambda series: np.sqrt((series**2).mean())),
            ACCEL_LON_RMS_COLUMN,
        )
        dt = lon["timestamp"].groupby(lon["_tick"]).diff().dt.total_seconds()
        jerk = (values.groupby(lon["_tick"]).diff() / dt.replace(0.0, np.nan)).abs()
        append_metric(jerk.groupby(lon["_tick"]).max(), JERK_MAX_COLUMN)

    lat = work.loc[work["source_channel"] == ACCEL_LAT_COLUMN].copy()
    if not lat.empty:
        lat = lat.sort_values("timestamp", kind="stable").drop_duplicates("timestamp", keep="last")
        values = pd.to_numeric(lat["value_number"], errors="coerce")
        append_metric(values.abs().groupby(lat["_tick"]).max(), ACCEL_LAT_MAX_COLUMN)

    ordinary = work.loc[~work["source_channel"].isin(raw_channels), frame.columns]
    return pd.concat([ordinary, *synthetic], ignore_index=True)


def to_wide(
    telemetry: pd.DataFrame,
    *,
    value: TelemetryValueKind = "value",
    index_columns: Sequence[str] = INDEX_COLUMNS,
    coerce_numeric: bool = True,
) -> pd.DataFrame:
    """Pivot long telemetry rows to one column per `source:channel`.

    The `value` column is `object`-dtype (it coalesces numeric / text / bool
    values per row), so the pivot output is `object` too — which means the
    downstream resampler and feature stages would treat even genuinely
    numeric channels (OBD PIDs, decoded CAN signals) as non-numeric and skip
    them. With `coerce_numeric=True` (the default), each value column whose
    non-null entries are all numbers is cast to a numeric dtype after the
    pivot. Channels carrying strings (VIN) or bools are left untouched.
    """
    if telemetry.empty:
        return pd.DataFrame(columns=[*index_columns])

    required = {*index_columns, "source_channel", value}
    missing = required - set(telemetry.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")

    pivoted = telemetry.pivot_table(
        index=list(index_columns),
        columns="source_channel",
        values=value,
        aggfunc="last",
        dropna=False,
    )
    pivoted.columns = [str(column) for column in pivoted.columns]
    wide = pivoted.reset_index().sort_values(list(index_columns), kind="stable")

    if coerce_numeric:
        index_set = set(index_columns)
        for column in wide.columns:
            if column in index_set:
                continue
            col = wide[column]
            if pd.api.types.is_numeric_dtype(col):
                # Already numeric (rare on the object-valued read_session
                # path; happens when to_wide is called with a numeric
                # `value` kind). Narrow float64 → float32; leave int / bool
                # channels untouched so their semantics don't change.
                if col.dtype == "float64":
                    wide[column] = col.astype("float32")
                continue
            converted = pd.to_numeric(col, errors="coerce")
            # Only keep the conversion if it didn't drop any real values —
            # i.e. every non-null original entry was numeric. A string-bearing
            # channel (VIN) or a bool channel stays as-is.
            original_notna = col.notna()
            if bool((original_notna == converted.notna()).all()):
                # Store as float32: telemetry is integer-origin sensor data
                # (≤16-bit raw range, simple scale/offset) — exactly
                # representable in a 24-bit mantissa, so this is lossless for
                # the values that matter. This is the dominant pre-resample
                # memory cost (the dense pivot); halving it is what actually
                # keeps full-corpus feature-building off the OOM line.
                # Scaler μ/σ still reduce in float64 (see normalization.py).
                wide[column] = converted.astype("float32")

    return wide


def numeric_value_columns(frame: pd.DataFrame) -> list[str]:
    """Numeric channel columns, excluding structural columns and gap flags.

    Canonical selection shared by every batch preprocessing path that walks
    from a wide/resampled/segmented frame to a feature-column list
    (sequence windowing, session classification). Two independent copies of
    this had drifted apart — one excluded ``__gap`` flag columns, the other
    didn't — which let boolean gap flags leak into session-classifier
    quality stats as if they were real feature channels.
    """
    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    return [
        str(column)
        for column in frame.columns
        if str(column) not in skip
        and not str(column).endswith(GAP_SUFFIX)
        and pd.api.types.is_numeric_dtype(frame[column])
    ]


def resample_and_segment(
    session_dirs: Iterable[Path],
    config: CanaryConfig,
    *,
    value: TelemetryValueKind = "value_number",
) -> pd.DataFrame:
    """Load session Parquet and run the shared ``read → wide → resample →
    segment`` chain used by every batch preprocessing entry point.

    Returns the segmented (but not yet feature-computed) frame. Empty input
    (no sessions, no rows, no numeric channels) flows through to an empty or
    minimal-column result rather than raising — callers decide what an empty
    result means for them (skip, quarantine, raise).
    """
    sessions = [Path(path) for path in session_dirs]
    resample_config = ResampleConfig(
        hz=config.preprocessing.resample_hz,
        max_gap_factor=config.preprocessing.max_gap_factor,
        slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
        session_gap_seconds=config.preprocessing.session_gap_seconds,
    )
    if value == "value_number":
        # Process one session at a time and concatenate only the reduced grids.
        # The previous chain pivoted millions of mostly-unique raw timestamps
        # before reducing them to 1 Hz, creating a multi-gigabyte sparse frame.
        reduced = [
            resample_long(
                _decimate_imu_long(
                    _read_session_numeric(session), hz=config.preprocessing.resample_hz
                ),
                config=resample_config,
            )
            for session in sessions
        ]
        resampled = pd.concat(reduced, ignore_index=True) if reduced else pd.DataFrame()
        return segment_operating_modes(
            resampled,
            config.preprocessing.operating_modes,
            mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        )

    long = read_sessions(sessions)
    wide = to_wide(long, value=value)
    # 2a.3 — collapse any raw-rate imu:ACCEL_LON/LAT into per-resample-tick
    # MAX/RMS/JERK_MAX aggregates before the resampler's own .last() would
    # otherwise pick one arbitrary raw sample per tick. No-op when neither
    # column is present (every session recorded before Phase 2b hardware).
    wide = decimate_imu_wide(wide, hz=config.preprocessing.resample_hz)
    value_columns = numeric_value_columns(wide)
    if value_columns:
        resampled = resample_wide(
            wide,
            config=resample_config,
            value_columns=value_columns,
        )
    else:
        resampled = pd.DataFrame(
            {
                SESSION_COLUMN: wide[SESSION_COLUMN].copy()
                if SESSION_COLUMN in wide.columns
                else pd.Series(dtype="object"),
                TIMESTAMP_COLUMN: pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True)
                if TIMESTAMP_COLUMN in wide.columns
                else pd.Series(dtype="datetime64[ns, UTC]"),
            }
        )
    return segment_operating_modes(
        resampled,
        config.preprocessing.operating_modes,
        recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
        mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
    )


def _coalesce_value_columns(frame: pd.DataFrame) -> pd.Series:
    value = frame["value_number"].astype("object")
    text_mask = frame["value_text"].notna()
    bool_mask = frame["value_bool"].notna()
    value.loc[text_mask] = frame.loc[text_mask, "value_text"]
    value.loc[bool_mask] = frame.loc[bool_mask, "value_bool"]
    return value


def _empty_long_frame() -> pd.DataFrame:
    columns: list[str] = [
        "timestamp",
        "source",
        "channel",
        "value_number",
        "value_text",
        "value_bool",
        "unit",
        "payload",
        "metadata_json",
        "session",
        "session_path",
        "source_channel",
        "value",
    ]
    return pd.DataFrame(columns=columns)
