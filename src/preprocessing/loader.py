"""Parquet session loading utilities for preprocessing.

**Every session is read with its ``decoded`` rows decoded on read (P02.8).**
The recorder stores raw CAN frames with their payloads and no ``decoded``
rows (operator decision 2026-09-26); sessions recorded before then also carry
stored ones. :func:`read_session`, :func:`iter_session_parts` and the
``value_number`` readers behind :func:`resample_and_segment` drop any stored
``decoded`` row and decode the frames with the current catalog instead
(:func:`src.preprocessing.decode.decode_on_read`), so a reader sees the same
rows from an old session and a new one, and cannot prefer a stale stored copy.
``decoded="as_recorded"`` is for the two kinds of reader that must see the
disk as written: the training chain, which recomputes every binding itself
(the recomputed row wins over a stored one), and storage accounting.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN, is_flag_column
from src.config import CanaryConfig
from src.decode.bindings import Bindings
from src.decode.on_read import can_channel_spellings, on_read_catalog
from src.decode.slices import sentinel_channel
from src.preprocessing.decode import decode_on_read, on_read_can_ids
from src.preprocessing.imu_decimation import (
    ACCEL_LAT_COLUMN,
    ACCEL_LAT_MAX_COLUMN,
    ACCEL_LON_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_MEAN_COLUMN,
    ACCEL_LON_RMS_COLUMN,
    JERK_MAX_COLUMN,
    decimate_imu_wide,
)
from src.preprocessing.resampler import resample_config_for, resample_wide
from src.preprocessing.segmenter import segment_operating_modes
from src.storage.broadcast_storage import FRAME_ATTR, frame_attrs, session_can_storage
from src.storage.session_files import PARQUET_PATTERN, footer_utc, has_telemetry
from src.telemetry import CAN_SOURCE, DECODED_SOURCE, SOURCE_SEP
from src.telemetry import channel_key as channel_key

log = logging.getLogger(__name__)

INDEX_COLUMNS = ["session", "timestamp"]
TelemetryValueKind = Literal["value", "value_number", "value_text", "value_bool", "payload"]
#: How a reader gets a session's ``decoded`` rows (module docstring):
#: ``on_read`` decodes them from the frames with the current catalog, whatever
#: the session stored; ``as_recorded`` returns the rows exactly as written.
DecodedRows = Literal["on_read", "as_recorded"]


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


def read_session(
    session_dir: Path, *, filters: Any = None, decoded: DecodedRows = "on_read"
) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame.

    ``filters`` is passed to ``pd.read_parquet`` (pyarrow DNF row filters) so
    a bounded reader can skip rows it will never use — e.g. the raw CAN
    frames of ids no requested binding decodes — without a second reader.

    ``decoded`` (module docstring): by default the session's ``decoded`` rows
    are decoded on read, and ``filters`` applies to them exactly as it applied
    to stored ones (the frames they come from are read for the decode even
    when ``filters`` excludes them, and dropped after it).
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    frames, skipped = _read_chunk_files(candidates, _ReadPlan.of(filters, decoded))
    _warn_skipped_chunks(session_dir, skipped)
    if not frames:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    return _with_can_storage(_finish_long_frame(frames, session_dir), session_dir, candidates)


def _with_can_storage(
    frame: pd.DataFrame, session_dir: Path, chunks: Sequence[Path]
) -> pd.DataFrame:
    """``frame`` carrying its session's CAN storage mode (P02.10) in
    ``attrs[FRAME_ATTR]``, from the chunks' footers: a reader handed the frame
    rather than the path still knows which broadcast rows are change-only."""
    frame.attrs[FRAME_ATTR] = frame_attrs(session_can_storage(session_dir, chunks))
    return frame


@dataclass(frozen=True)
class _ReadPlan:
    """What one session read asks parquet for, and what it does after.

    ``read_filters`` is ``filters`` widened, when a decoded row could pass
    ``filters``, by the frames of the on-read catalog's ids; ``refilter``
    then applies ``filters`` again after the decode, to the decoded rows and
    to the widening's frames.
    """

    filters: Any
    read_filters: Any
    catalog: Bindings | None
    on_read: bool
    refilter: bool

    @classmethod
    def of(cls, filters: Any, decoded: DecodedRows) -> _ReadPlan:
        if decoded == "as_recorded":
            return cls(filters, filters, None, on_read=False, refilter=False)
        if decoded != "on_read":
            raise ValueError(f"decoded must be 'on_read' or 'as_recorded', not {decoded!r}")
        catalog = on_read_catalog()
        if filters is None or catalog is None or not _admits_decoded(filters, catalog):
            return cls(filters, filters, catalog, on_read=True, refilter=False)
        frames = [
            ("source", "==", CAN_SOURCE),
            ("channel", "in", can_channel_spellings(on_read_can_ids(catalog))),
        ]
        widened = [*_disjunction(filters), frames]
        return cls(filters, widened, catalog, on_read=True, refilter=True)

    def apply(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Decode ``frame`` on read (a batch of whole chunks) and refilter."""
        if not self.on_read:
            return frame
        frame = decode_on_read(frame, self.catalog)
        if self.refilter and not frame.empty:
            frame = frame.loc[_dnf_mask(frame, self.filters)]
        return frame


def _disjunction(filters: Any) -> list[list[tuple[str, str, Any]]]:
    """pyarrow DNF ``filters`` (one conjunction, or a list of them) as a list
    of conjunctions."""
    items = list(filters)
    if items and isinstance(items[0], tuple):
        return [list(items)]
    return [list(conjunction) for conjunction in items]


def _scalar_predicate(value: Any, op: str, operand: Any) -> bool:
    return bool(_predicate(pd.Series([value], dtype=object), op, operand)[0])


def _predicate(column: pd.Series, op: str, operand: Any) -> np.ndarray[Any, np.dtype[Any]]:
    """One DNF predicate over a column, with pyarrow's null rule (a null never
    matches, ``!=`` and ``not in`` included). Refuses an operator it does not
    know rather than guess what pyarrow did with it."""
    if op in ("=", "=="):
        matched = column == operand
    elif op == "!=":
        matched = column != operand
    elif op == "<":
        matched = column < operand
    elif op == "<=":
        matched = column <= operand
    elif op == ">":
        matched = column > operand
    elif op == ">=":
        matched = column >= operand
    elif op == "in":
        matched = column.isin(list(operand))
    elif op == "not in":
        matched = ~column.isin(list(operand))
    else:
        raise ValueError(f"filter operator {op!r} is not supported on a decode-on-read read")
    return np.asarray(matched.to_numpy(dtype=bool) & column.notna().to_numpy(dtype=bool))


def _dnf_mask(frame: pd.DataFrame, filters: Any) -> np.ndarray[Any, np.dtype[Any]]:
    """Rows of ``frame`` that pass pyarrow DNF ``filters``."""
    passed = np.zeros(len(frame), dtype=bool)
    for conjunction in _disjunction(filters):
        matched = np.ones(len(frame), dtype=bool)
        for column, op, operand in conjunction:
            matched &= _predicate(frame[column], op, operand)
        passed |= matched
    return passed


def _admits_decoded(filters: Any, catalog: Bindings) -> bool:
    """Could a ``decoded`` row of ``catalog`` pass ``filters``?

    Decided on ``source`` and ``channel`` predicates only; a predicate on any
    other column counts as passable, so this may say yes needlessly (the
    refilter then drops the rows) but never no wrongly.
    """
    names = [s.name for s in catalog.signals] + [sentinel_channel(s.name) for s in catalog.signals]
    for conjunction in _disjunction(filters):
        passable = True
        for column, op, operand in conjunction:
            if column == "source":
                passable &= _scalar_predicate(DECODED_SOURCE, op, operand)
            elif column == "channel":
                passable &= any(_scalar_predicate(name, op, operand) for name in names)
        if passable:
            return True
    return False


def _read_chunk_files(
    paths: Sequence[Path], plan: _ReadPlan | None = None
) -> tuple[list[pd.DataFrame], list[str]]:
    """Read ``paths`` in the order given; returns the frames and skipped names.

    A daemon crash mid-write leaves trailing 0-byte or truncated chunks (the
    atomic writer's target appears before its content lands). One bad tail
    chunk must not poison the whole training corpus — skip the unreadable
    ones and keep going.

    ``plan`` (default: as recorded, unfiltered) decodes on read in batches of
    whole chunks of about :data:`SESSION_PART_ROWS` rows: the decode is
    row-local, so batching changes nothing but the per-call overhead.
    """
    plan = plan if plan is not None else _ReadPlan.of(None, "as_recorded")
    frames: list[pd.DataFrame] = []
    skipped: list[str] = []
    batch: list[pd.DataFrame] = []
    batch_rows = 0

    def flush() -> None:
        nonlocal batch, batch_rows
        if batch:
            frames.append(plan.apply(pd.concat(batch, ignore_index=True)))
            batch, batch_rows = [], 0

    for path in paths:
        if path.stat().st_size == 0:
            skipped.append(path.name)
            continue
        try:
            frame = pd.read_parquet(path, filters=plan.read_filters)
        except (OSError, ValueError, pd.errors.ParserError):
            skipped.append(path.name)
            continue
        if not plan.on_read:
            frames.append(frame)
            continue
        batch.append(frame)
        batch_rows += len(frame)
        if batch_rows >= SESSION_PART_ROWS:
            flush()
    flush()
    if plan.on_read:
        _return_decode_memory()
    return frames, skipped


def _return_decode_memory() -> None:
    """Hand Arrow's freed decode temporaries back to the OS.

    The decode on read allocates and frees many short-lived Arrow buffers
    (string columns built by ``take``); the pool (mimalloc) keeps the freed
    pages, and what a reader does next then allocates beside them. On
    ``2026-09-18T144215`` that kept ~240 MB and took the digest chain's peak
    from 1,492 MB to 1,764 MB; releasing it here brings it back to ~1,500 MB
    (2026-09-26).
    """
    import pyarrow as pa

    pa.default_memory_pool().release_unused()


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
    session_dir: Path,
    *,
    target_rows: int = SESSION_PART_ROWS,
    filters: Any = None,
    decoded: DecodedRows = "on_read",
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
    readable chunk. ``filters`` and ``decoded`` are :func:`read_session`'s,
    applied to every part (grouping still uses every row's footer
    statistics). A decoded row lands in its frame's part, which is exact: the
    decode is row-local and shares its frame's timestamp.
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    groups, skipped = _time_disjoint_chunk_groups(candidates, target_rows)
    plan = _ReadPlan.of(filters, decoded)
    yielded = False
    for group in groups:
        frames, unreadable = _read_chunk_files(group, plan)
        skipped.extend(unreadable)
        if frames:
            yielded = True
            yield _with_can_storage(
                _finish_long_frame(frames, session_dir), session_dir, candidates
            )
    _warn_skipped_chunks(session_dir, sorted(skipped))
    if not yielded:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")


def iter_session_numeric_parts(
    session_dir: Path,
    *,
    target_rows: int = SESSION_PART_ROWS,
    align_ns: int,
    on_read: Bindings | None | Literal["current"] = "current",
) -> Iterator[pd.DataFrame]:
    """The ``value_number`` chain's rows (:func:`_read_numeric_chunks`), in
    time-disjoint parts: the only way that chain reads a session (P02.1).

    Chunks are grouped as :func:`iter_session_parts` groups them, and a part
    ends only on an ``align_ns`` bin edge (epoch-floored), so a per-tick
    aggregate (:func:`_decimate_imu_long`) never sees half a tick. The edge is
    made by carrying each part's last, possibly incomplete, bin into the next
    part rather than by requiring chunk boundaries to fall on one: the writer
    starts a chunk right after the last, in the same second almost always, so
    requiring an aligned chunk boundary left ``2026-09-18T144215`` (558 chunks)
    in one part and ``2026-09-20T152758`` (1,980) in two -- the whole session
    in memory, which P02.1 measured before fixing it. Each part's rows keep
    their whole-session relative order for any one channel and timestamp, the
    only order ``resample_long`` reads. Unreadable chunks are skipped silently,
    as this chain always has.

    ``on_read`` is the catalog the ``decoded`` rows are decoded with
    (:func:`_read_numeric_chunks`); ``"current"`` loads it, and a cache that
    versions by it (the derived store) passes the one it hashed.
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    groups, _skipped = _time_disjoint_chunk_groups(candidates, target_rows)
    catalog = on_read_catalog() if on_read == "current" else on_read
    carry: pd.DataFrame | None = None
    for group in groups:
        frames = _read_numeric_chunks(group, catalog)
        if not frames:
            continue
        frame = pd.concat(frames, ignore_index=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame["session"] = session_dir.name
        frame["source_channel"] = (
            frame["source"].astype(str) + SOURCE_SEP + frame["channel"].astype(str)
        )
        frame = frame[["session", "timestamp", "source_channel", "value_number"]]
        if carry is not None:
            # Every carried row is earlier than this group's first (groups are
            # time-disjoint and in time order), so the stable sort keeps both
            # sides' order.
            frame = pd.concat([carry, frame], ignore_index=True)
        frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
        stamps = pd.DatetimeIndex(frame["timestamp"])
        ns = stamps.as_unit("ns").tz_localize(None).to_numpy().view(np.int64)
        valid = int(np.count_nonzero(stamps.notna()))  # NaT sorts last
        split = (
            int(np.searchsorted(ns[:valid], int(ns[valid - 1]) // align_ns * align_ns))
            if valid
            else 0
        )
        carry = frame.iloc[split:]
        if split:
            yield frame.iloc[:split].reset_index(drop=True)
    if carry is None:
        raise FileNotFoundError(f"no readable {PARQUET_PATTERN} files in {session_dir}")
    yield carry.reset_index(drop=True)


#: The columns the ``value_number`` chain keeps.
_NUMERIC_COLUMNS = ["timestamp", "source", "channel", "value_number"]


def _read_numeric_chunks(paths: Sequence[Path], catalog: Bindings | None) -> list[pd.DataFrame]:
    """The ``value_number`` columns of ``paths``, with ``decoded`` rows decoded
    on read (module docstring) in batches of about :data:`SESSION_PART_ROWS`.

    ``payload`` is read beside those columns for the decode and dropped after
    it, so the frames hold no payload bytes. Before P02.8 this chain took the
    stored ``decoded`` rows' ``value_number`` as they were; the rows it gets
    now are the same ones, for sessions that never stored them too.
    Unreadable chunks are skipped silently, as this chain always has.
    """
    frames: list[pd.DataFrame] = []
    batch: list[pd.DataFrame] = []
    batch_rows = 0

    def flush() -> None:
        nonlocal batch, batch_rows
        if batch:
            frame = decode_on_read(pd.concat(batch, ignore_index=True), catalog)
            frames.append(frame[_NUMERIC_COLUMNS])
            batch, batch_rows = [], 0

    for path in paths:
        if path.stat().st_size == 0:
            continue
        try:
            frame = pd.read_parquet(path, columns=[*_NUMERIC_COLUMNS, "payload"])
        except (OSError, ValueError, pd.errors.ParserError):
            try:  # a chunk written before the payload column existed
                frame = pd.read_parquet(path, columns=_NUMERIC_COLUMNS)
            except (OSError, ValueError, pd.errors.ParserError):
                continue
        batch.append(frame)
        batch_rows += len(frame)
        if batch_rows >= SESSION_PART_ROWS:
            flush()
    flush()
    _return_decode_memory()
    return frames


def _time_disjoint_chunk_groups(
    candidates: Sequence[Path], target_rows: int
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
        if current and current_hi is not None and lo > current_hi and current_rows >= target_rows:
            groups.append(sorted(current, key=lambda p: p.name))
            current, current_hi, current_rows = [], None, 0
        current.append(path)
        current_hi = hi if current_hi is None or hi > current_hi else current_hi
        current_rows += rows
    if current:
        groups.append(sorted(current, key=lambda p: p.name))
    return groups, skipped


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
        low, high = footer_utc(stats.min), footer_utc(stats.max)
        if low is None or high is None:
            return None
        lo = low if lo is None or low < lo else lo
        hi = high if hi is None or high > hi else hi
    if lo is None or hi is None:
        return None
    return lo, hi


def read_sessions(
    session_dirs: Iterable[Path], *, decoded: DecodedRows = "on_read"
) -> pd.DataFrame:
    """Read several session directories into one long-format DataFrame."""
    dirs = list(session_dirs)
    frames = [read_session(path, decoded=decoded) for path in dirs]
    if not frames:
        return _empty_long_frame()
    out = pd.concat(frames, ignore_index=True).sort_values(
        ["session", "timestamp", "source", "channel"],
        kind="stable",
    )
    out.attrs[FRAME_ATTR] = frame_attrs([session_can_storage(path) for path in dirs])
    return out


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
        # The one signed aggregate (H04.4's committee member): NaN, never 0,
        # for a tick without a finite sample.
        append_metric(grouped.mean(), ACCEL_LON_MEAN_COLUMN)
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
    # P02.10: the resampler refuses a broadcast class shorter than the timeout
    # the rows were stored with; the pivot drops ``attrs``, so carry it.
    if FRAME_ATTR in telemetry.attrs:
        wide.attrs[FRAME_ATTR] = telemetry.attrs[FRAME_ATTR]
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
        # One session at a time, each read part by part (P02.1): a part of
        # ~SESSION_PART_ROWS is reduced to the cells the resampler reads before
        # the next is read, so memory is one part plus the session's reduced
        # cells, never the whole session. That holds with or without a store:
        # the store (laptop tools, P01.2) only reads back the same cells
        # instead of rebuilding them. It is never active on the Pi
        # (``active_store``), where the digest and classification children run
        # this same bounded chain.
        # H02.4: each session's resolved imu_frame.json adds imu:ACCEL_LON/LAT
        # inside that reduction (``imu_stream``), and ``attrs[IMU_FRAME_ATTR]``
        # says per session whether it did and, if not, which kind of absent.
        from src.preprocessing.derived import reduce_numeric_session, resample_numeric_reduced
        from src.preprocessing.derived_store import active_store
        from src.preprocessing.imu_stream import IMU_FRAME_ATTR, plan_imu_frame

        store = active_store()
        reduced: list[pd.DataFrame] = []
        imu_frames: dict[str, dict[str, Any]] = {}
        for session in sessions:
            plan = plan_imu_frame(session)
            session_cells = (
                store.load_numeric(session, resample_config)[0]
                if store is not None
                else reduce_numeric_session(session, resample_config, imu_plan=plan)
            )
            use = plan.use(session_cells.channels)
            if use.status in ("unresolved", "stale", "refused"):
                log.info("%s: IMU frame not applied (%s): %s", use.session, use.status, use.reason)
            imu_frames[use.session] = use.as_dict()
            reduced.append(resample_numeric_reduced(session_cells, resample_config))
            del session_cells
        resampled = pd.concat(reduced, ignore_index=True) if reduced else pd.DataFrame()
        segmented = segment_operating_modes(
            resampled,
            config.preprocessing.operating_modes,
            recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
            mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        )
        segmented.attrs[IMU_FRAME_ATTR] = imu_frames
        return segmented

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
