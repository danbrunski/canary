"""Parquet session loading utilities for preprocessing."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    is_flag_column,
    resample_config_for,
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


def has_telemetry(session_dir: Path) -> bool:
    """True when ``session_dir`` holds at least one non-empty telemetry chunk.

    The one "does this session have data?" predicate (R06.6). Scripts had
    drifted: some counted any chunk, some only non-empty ones. A directory
    exists from the moment the writer rotates into it, before its first chunk
    flushes, and a zero-byte chunk is not data.
    """
    return any(f.stat().st_size > 0 for f in session_dir.glob(PARQUET_PATTERN))


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
        if child.is_dir() and not child.name.startswith("_") and has_telemetry(child)
    )


@dataclass(frozen=True)
class SessionFooterSpan:
    """What a session's Parquet footers say without reading any data.

    ``first``/``last`` are the ``timestamp`` column's row-group min/max over
    every chunk (UTC); ``rows`` is the summed ``num_rows``. Each is ``None``
    when unknown -- nothing readable, or chunks written without statistics --
    never zero.
    """

    first: pd.Timestamp | None
    last: pd.Timestamp | None
    rows: int | None

    @property
    def span_seconds(self) -> float | None:
        if self.first is None or self.last is None:
            return None
        return float((self.last - self.first).total_seconds())


def session_footer_span(session_dir: Path) -> SessionFooterSpan:
    """One walk of a session's Parquet footers: time span and row count.

    The one footer reader (R05.14): :func:`count_session_rows`,
    :func:`session_time_span_seconds` and ``analysis.workbench.session_span``
    each walked the footers themselves. A truncated or 0-byte chunk (a
    daemon crash mid-write) just doesn't count.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency elsewhere
        return SessionFooterSpan(None, None, None)
    lo: pd.Timestamp | None = None
    hi: pd.Timestamp | None = None
    rows = 0
    seen = False
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            if chunk.stat().st_size == 0:
                continue
            # pyarrow's typing of ParquetFile.__init__ varies by version (untyped
            # in some, annotated in others) — unused-ignore keeps this line green
            # under both, since CI and local envs can resolve different pyarrow.
            meta = pq.ParquetFile(chunk).metadata  # type: ignore[no-untyped-call, unused-ignore]
        except Exception:  # noqa: BLE001 - a corrupt/truncated chunk just doesn't count
            continue
        rows += meta.num_rows
        seen = True
        names = list(meta.schema.names)
        if "timestamp" not in names:
            continue
        column = names.index("timestamp")
        for group in range(meta.num_row_groups):
            stats = meta.row_group(group).column(column).statistics
            if stats is None or not stats.has_min_max:
                continue
            low, high = _footer_utc(stats.min), _footer_utc(stats.max)
            if low is not None and (lo is None or low < lo):
                lo = low
            if high is not None and (hi is None or high > hi):
                hi = high
    return SessionFooterSpan(first=lo, last=hi, rows=rows if seen else None)


def _footer_utc(value: Any) -> pd.Timestamp | None:
    """A footer statistic as a UTC timestamp (naive statistics are UTC)."""
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def count_session_rows(session_dir: Path) -> int | None:
    """Row count of a session's Parquet chunks from footer metadata only.

    No data read, so this is cheap enough to call before a replay starts
    (D02.9 reporting) or while indexing a session. ``None`` means "unknown"
    (nothing readable) as distinct from a genuine zero.
    """
    return session_footer_span(session_dir).rows


def session_time_span_seconds(session_dir: Path) -> float | None:
    """Recorded span of a session in seconds, from Parquet footer statistics.

    Reads only the ``timestamp`` column's row-group min/max, so like
    :func:`count_session_rows` it is cheap enough to run before a replay
    starts. A "1x" replay clamps every recorded gap to
    :data:`src.collection.replay._MAX_GAP_SECONDS`, so wall time says nothing
    about how much of the recording has been replayed; this is the
    denominator that does (D02.9). ``None`` when no chunk carries statistics.
    """
    return session_footer_span(session_dir).span_seconds


def read_session(session_dir: Path, *, filters: Any = None) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame.

    ``filters`` is passed to ``pd.read_parquet`` (pyarrow DNF row filters) so
    a bounded reader can skip rows it will never use — e.g. the raw CAN
    frames of ids no requested binding decodes — without a second reader.
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    frames, skipped = _read_chunk_files(candidates, filters=filters)
    _warn_skipped_chunks(session_dir, skipped)
    if not frames:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    return _finish_long_frame(frames, session_dir)


def _read_chunk_files(
    paths: Sequence[Path], *, filters: Any = None
) -> tuple[list[pd.DataFrame], list[str]]:
    """Read ``paths`` in the order given; returns the frames and skipped names.

    A daemon crash mid-write leaves trailing 0-byte or truncated chunks (the
    atomic writer's target appears before its content lands). One bad tail
    chunk must not poison the whole training corpus — skip the unreadable
    ones and keep going.
    """
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    for path in paths:
        if path.stat().st_size == 0:
            skipped.append(path.name)
            continue
        try:
            frames.append(pd.read_parquet(path, filters=filters))
        except (OSError, ValueError, pd.errors.ParserError):
            skipped.append(path.name)
    return frames, skipped


def _warn_skipped_chunks(session_dir: Path, skipped: Sequence[str]) -> None:
    if skipped:
        log.warning(
            "skipped %d unreadable chunk(s) in %s: %s",
            len(skipped),
            session_dir.name,
            ", ".join(skipped),
        )


def _finish_long_frame(frames: Sequence[pd.DataFrame], session_dir: Path) -> pd.DataFrame:
    """Concatenate chunk frames (in chunk-name order) into :func:`read_session`'s shape."""
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


#: Raw rows per :func:`iter_session_parts` part. A long frame costs ~800 bytes
#: a row once read (object ``value``, a ``source_channel`` string per row) and
#: roughly 2.5x that after CAN decode. A 2 h drive is 17.9M rows: read whole,
#: that alone was ~12 GB, and the A02.11 baseline was OOM-killed at 15 GB
#: decoding it. Measured on ``2026-09-18T213818`` (10.6M rows), whole-session
#: peak RSS of the reduced path by part size: 500k rows 2.44 GB, 200k 1.46 GB,
#: 100k 1.04 GB, at the same wall time (2026-09-26).
SESSION_PART_ROWS = 100_000


def iter_session_parts(
    session_dir: Path, *, target_rows: int = SESSION_PART_ROWS, filters: Any = None
) -> Iterator[pd.DataFrame]:
    """:func:`read_session`, yielded in time-disjoint parts of ~``target_rows``.

    Each part is exactly the rows :func:`read_session` would return for its
    time range, in the same order: chunks are grouped by their footer
    ``timestamp`` statistics, a part ends only where the next chunk starts
    strictly after every timestamp already in it, and each part's chunks are
    concatenated in chunk-name order before the same stable sort. So no
    timestamp is split between parts, rows tied on the sort key keep their
    whole-session order, and concatenating the parts gives ``read_session``'s
    frame (index aside). A readable chunk without statistics puts the whole
    session in one part: correct, just not bounded.

    Raises ``FileNotFoundError`` as :func:`read_session` does when there is no
    readable chunk. ``filters`` is :func:`read_session`'s row filter, applied
    to every part (grouping still uses every row's footer statistics).
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    groups, skipped = _time_disjoint_chunk_groups(candidates, target_rows)
    yielded = False
    for group in groups:
        frames, unreadable = _read_chunk_files(group, filters=filters)
        skipped.extend(unreadable)
        if frames:
            yielded = True
            yield _finish_long_frame(frames, session_dir)
    _warn_skipped_chunks(session_dir, sorted(skipped))
    if not yielded:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")


def iter_session_numeric_parts(
    session_dir: Path, *, target_rows: int = SESSION_PART_ROWS, align_ns: int
) -> Iterator[pd.DataFrame]:
    """:func:`_read_session_numeric`, yielded in time-disjoint parts.

    Same grouping as :func:`iter_session_parts`, but a part also ends only on
    an ``align_ns`` bin edge (epoch-floored), so a per-tick aggregate
    (:func:`_decimate_imu_long`) never sees half a tick. Each part's rows keep
    their whole-session relative order for any one channel and timestamp, the
    only order ``resample_long`` reads. Unreadable chunks are skipped silently,
    as :func:`_read_session_numeric` skips them.
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    groups, _skipped = _time_disjoint_chunk_groups(candidates, target_rows, align_ns=align_ns)
    columns = ["timestamp", "source", "channel", "value_number"]
    yielded = False
    for group in groups:
        frames: list[pd.DataFrame] = []
        for path in group:
            if path.stat().st_size == 0:
                continue
            try:
                frames.append(pd.read_parquet(path, columns=columns))
            except (OSError, ValueError, pd.errors.ParserError):
                continue
        if not frames:
            continue
        yielded = True
        frame = pd.concat(frames, ignore_index=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["session"] = session_dir.name
        frame["source_channel"] = (
            frame["source"].astype(str) + SOURCE_SEP + frame["channel"].astype(str)
        )
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
        yield frame[["session", "timestamp", "source_channel", "value_number"]]
    if not yielded:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")


def _time_disjoint_chunk_groups(
    candidates: Sequence[Path], target_rows: int, *, align_ns: int | None = None
) -> tuple[list[list[Path]], list[str]]:
    """Group chunks into time-disjoint parts for :func:`iter_session_parts`.

    Returns the groups (each in chunk-name order, groups in time order) and
    the names of chunks already known unreadable (0-byte, or a footer that
    will not parse, which ``pd.read_parquet`` cannot read either).
    """
    import pyarrow.parquet as pq

    spans: list[tuple[pd.Timestamp, pd.Timestamp, int, Path]] = []
    skipped: list[str] = []
    for path in candidates:
        if path.stat().st_size == 0:
            skipped.append(path.name)
            continue
        try:
            meta = pq.ParquetFile(path).metadata  # type: ignore[no-untyped-call, unused-ignore]
        except Exception:  # noqa: BLE001 - unreadable footer: read_parquet fails the same way
            skipped.append(path.name)
            continue
        span = _chunk_time_span(meta)
        if span is None:
            return [list(candidates)], []  # unordered data: one unbounded part
        spans.append((span[0], span[1], int(meta.num_rows), path))

    spans.sort(key=lambda item: (item[0], item[3].name))
    groups: list[list[Path]] = []
    current: list[Path] = []
    current_hi: pd.Timestamp | None = None
    current_rows = 0
    for lo, hi, rows, path in spans:
        if (
            current
            and current_hi is not None
            and _starts_after(lo, current_hi, align_ns)
            and current_rows >= target_rows
        ):
            groups.append(sorted(current, key=lambda p: p.name))
            current, current_hi, current_rows = [], None, 0
        current.append(path)
        current_hi = hi if current_hi is None or hi > current_hi else current_hi
        current_rows += rows
    if current:
        groups.append(sorted(current, key=lambda p: p.name))
    return groups, skipped


def _starts_after(lo: pd.Timestamp, hi: pd.Timestamp, align_ns: int | None) -> bool:
    """``lo`` is strictly after ``hi`` (and in a later ``align_ns`` bin, if given)."""
    if align_ns is None:
        return bool(lo > hi)
    return bool(lo.value // align_ns > hi.value // align_ns)


def _chunk_time_span(meta: Any) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """One chunk's ``timestamp`` min/max from its footer; ``None`` when any
    row group lacks statistics."""
    names = list(meta.schema.names)
    if "timestamp" not in names or meta.num_row_groups == 0:
        return None
    column = names.index("timestamp")
    lo: pd.Timestamp | None = None
    hi: pd.Timestamp | None = None
    for group in range(meta.num_row_groups):
        stats = meta.row_group(group).column(column).statistics
        if stats is None or not stats.has_min_max:
            return None
        low, high = _footer_utc(stats.min), _footer_utc(stats.max)
        if low is None or high is None:
            return None
        lo = low if lo is None or low < lo else lo
        hi = high if hi is None or high > hi else hi
    if lo is None or hi is None:
        return None
    return lo, hi


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
        coerce_numeric_columns(wide, index_columns=index_columns)

    return wide


def non_numeric_columns(
    wide: pd.DataFrame, *, index_columns: Sequence[str] = INDEX_COLUMNS
) -> set[str]:
    """Object columns :func:`coerce_numeric_columns` would leave un-coerced:
    some non-null entry is not a number (VIN text, a bool channel)."""
    index_set = set(index_columns)
    out: set[str] = set()
    for column in wide.columns:
        if column in index_set or pd.api.types.is_numeric_dtype(wide[column]):
            continue
        col = wide[column]
        converted = pd.to_numeric(col, errors="coerce")
        if not bool((col.notna() == converted.notna()).all()):
            out.add(str(column))
    return out


def coerce_numeric_columns(
    wide: pd.DataFrame,
    *,
    index_columns: Sequence[str] = INDEX_COLUMNS,
    keep_object: Iterable[str] = (),
) -> pd.DataFrame:
    """Cast every all-numeric value column of ``wide`` to float32, in place.

    :func:`to_wide`'s ``coerce_numeric`` step, split out so the bounded
    session loader applies the identical rule part by part. ``keep_object``
    names columns to leave as they are: a column that is all-numeric in one
    part but carries text elsewhere in the session stays ``object``, as the
    whole-session pivot leaves it. Returns ``wide``.
    """
    index_set = set(index_columns)
    held = set(keep_object)
    for column in wide.columns:
        if column in index_set or column in held:
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
        and not is_flag_column(column)
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
    resample_config = resample_config_for(config)
    if value == "value_number":
        # Process one session at a time and concatenate only the reduced grids.
        # The previous chain pivoted millions of mostly-unique raw timestamps
        # before reducing them to 1 Hz, creating a multi-gigabyte sparse frame.
        # With a derived store active (laptop tools, P01.2) each session's
        # reduced cells are read back or built instead; the output is the same.
        # The store is never active on the Pi (``active_store``), where the
        # digest and workbench children keep this in-memory path.
        from src.preprocessing.derived import resample_numeric_reduced
        from src.preprocessing.derived_store import active_store

        store = active_store()
        reduced = [
            resample_numeric_reduced(
                store.load_numeric(session, resample_config)[0], resample_config
            )
            if store is not None
            else resample_long(
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
