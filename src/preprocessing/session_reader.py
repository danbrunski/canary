"""Stored-session readers: raw Parquet scanning and chunk planning (R13).

**Every session is read with its ``decoded`` rows decoded on read (P02.8).**
The recorder stores raw CAN frames with their payloads and no ``decoded``
rows (operator decision 2026-09-26); sessions recorded before then also carry
stored ones. :func:`read_session`, :func:`iter_session_parts` and the
``value_number`` reader :func:`iter_session_numeric_parts` drop any stored
``decoded`` row and decode the frames with the current catalog instead
(:func:`src.preprocessing.decode.decode_on_read`), so a reader sees the same
rows from an old session and a new one, and cannot prefer a stale stored copy.
``decoded="as_recorded"`` is for the two kinds of reader that must see the
disk as written: the training chain, which recomputes every binding itself
(the recomputed row wins over a stored one), and storage accounting.

This module is the bottom of the stored-row chain: it depends on the frame
contracts (``session_contracts``), the row-local decode (``decode``) and the
storage primitives, and on nothing that reduces, caches, resamples or reads
the IMU frame. Those sit above it and import it, never the other way
(``tests/test_preprocessing_boundaries.py``). ``loader`` re-exports these
names as a compatibility facade; nothing inside the package imports it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.decode.bindings import Bindings
from src.decode.on_read import can_channel_spellings, on_read_catalog
from src.decode.slices import sentinel_channel
from src.preprocessing.decode import decode_on_read, on_read_can_ids
from src.preprocessing.session_contracts import (
    KEY_COLUMNS,
    NUMERIC_COLUMNS,
    STORED_COLUMNS,
    VALUE_COLUMNS,
    DecodedRows,
)
from src.storage.broadcast_storage import FRAME_ATTR, frame_attrs, session_can_storage
from src.storage.session_files import (
    PARQUET_PATTERN,
    chunks_overlapping,
    footer_utc,
    has_telemetry,
)
from src.telemetry import CAN_SOURCE, DECODED_SOURCE, SOURCE_SEP

log = logging.getLogger(__name__)


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
    session_dir: Path,
    *,
    filters: Any = None,
    decoded: DecodedRows = "on_read",
    time_range: tuple[pd.Timestamp, pd.Timestamp] | None = None,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Read one telemetry session directory into a long-format DataFrame.

    ``filters`` is passed to ``pd.read_parquet`` (pyarrow DNF row filters) so
    a bounded reader can skip rows it will never use — e.g. the raw CAN
    frames of ids no requested binding decodes — without a second reader.

    ``decoded`` (module docstring): by default the session's ``decoded`` rows
    are decoded on read, and ``filters`` applies to them exactly as it applied
    to stored ones (the frames they come from are read for the decode even
    when ``filters`` excludes them, and dropped after it).

    ``time_range`` (UTC ``lo``, ``hi``) skips every chunk whose footer
    ``timestamp`` range lies outside it, before opening its data (H07.9: a
    20 s window used to open all ~1,200 chunks of a drive). It prunes chunks
    only; pair it with a row filter on ``timestamp`` for the exact window.
    A window no chunk reaches returns an empty frame.

    ``columns`` names the stored columns (:data:`STORED_COLUMNS`) the caller
    uses; the others are never read (P02.2: ``payload`` is a fifth of a
    session's bytes, and a polled-channel read never looks at it). The sort
    key (:data:`KEY_COLUMNS`) is always read, ``session``, ``session_path``
    and ``source_channel`` are always added, and ``value`` only when all
    three value columns are read. A decode on read still reads ``payload``
    for the frames it decodes and drops it afterwards unless it was asked
    for, so a narrowed read is the whole read's rows, restricted to its
    columns -- never fewer rows. A name outside the stored schema is refused.
    """
    candidates = sorted(session_dir.glob(PARQUET_PATTERN))
    if not candidates:
        raise FileNotFoundError(f"no {PARQUET_PATTERN} files in {session_dir}")
    plan = _ReadPlan.of(filters, decoded, columns)
    reading = candidates if time_range is None else chunks_overlapping(candidates, *time_range)
    if not reading:
        return _with_can_storage(_empty_long_frame(plan.columns), session_dir, candidates)
    frames, skipped = _read_chunk_files(reading, plan)
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


def _stored_columns(columns: Sequence[str] | None) -> tuple[str, ...] | None:
    """``columns`` plus the sort key, in schema order; ``None`` reads all."""
    if columns is None:
        return None
    unknown = sorted(set(columns) - set(STORED_COLUMNS))
    if unknown:
        raise ValueError(
            f"read_session columns must be stored columns {STORED_COLUMNS}; not {unknown}"
        )
    wanted = set(columns) | set(KEY_COLUMNS)
    return tuple(name for name in STORED_COLUMNS if name in wanted)


@dataclass(frozen=True)
class _ReadPlan:
    """What one session read asks parquet for, and what it does after.

    ``read_filters`` is ``filters`` widened, when a decoded row could pass
    ``filters``, by the frames of the on-read catalog's ids; ``refilter``
    then applies ``filters`` again after the decode, to the decoded rows and
    to the widening's frames. When no decoded row can pass ``filters``,
    ``catalog`` is ``None``: the read decodes nothing (stored ``decoded`` rows
    are still dropped), so a filtered read returns only rows that pass its
    filters (P02.18).

    ``columns`` is what the caller gets (``None``: all); ``read_columns`` adds
    ``payload`` when a catalog frame could pass ``read_filters``, because the
    decode on read needs it (without it the decode silently adds no rows), and
    ``apply`` drops it again after the decode.
    """

    filters: Any
    read_filters: Any
    catalog: Bindings | None
    on_read: bool
    refilter: bool
    columns: tuple[str, ...] | None = None
    read_columns: tuple[str, ...] | None = None

    @classmethod
    def of(
        cls, filters: Any, decoded: DecodedRows, columns: Sequence[str] | None = None
    ) -> _ReadPlan:
        wanted = _stored_columns(columns)
        if decoded == "as_recorded":
            return cls(filters, filters, None, False, False, wanted, wanted)
        if decoded != "on_read":
            raise ValueError(f"decoded must be 'on_read' or 'as_recorded', not {decoded!r}")
        catalog = on_read_catalog()
        if catalog is not None and filters is not None and not _admits_decoded(filters, catalog):
            # No decoded row can pass ``filters``: decode none (P02.18; a
            # can-only read used to come back with a decoded row per frame).
            # Stored decoded rows that pass are still dropped by the decode.
            catalog = None
        if filters is None or catalog is None:
            read_filters, refilter = filters, False
        else:
            frames = [
                ("source", "==", CAN_SOURCE),
                ("channel", "in", can_channel_spellings(on_read_can_ids(catalog))),
            ]
            read_filters, refilter = [*_disjunction(filters), frames], True
        read_columns = wanted
        if (
            wanted is not None
            and "payload" not in wanted
            and catalog is not None
            and _admits_catalog_frames(read_filters, catalog)
        ):
            read_columns = _stored_columns([*wanted, "payload"])
        return cls(filters, read_filters, catalog, True, refilter, wanted, read_columns)

    def apply(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Decode ``frame`` on read (a batch of whole chunks) and refilter."""
        if not self.on_read:
            return frame
        frame = decode_on_read(frame, self.catalog)
        if self.refilter and not frame.empty:
            frame = frame.loc[_dnf_mask(frame, self.filters)]
        if self.columns is not None and self.read_columns != self.columns:
            frame = frame.loc[:, [c for c in frame.columns if c in self.columns]]
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


def _admits_catalog_frames(filters: Any, catalog: Bindings) -> bool:
    """Could a raw CAN frame that ``catalog`` decodes pass ``filters``?

    As :func:`_admits_decoded`, decided on ``source`` and ``channel`` only and
    never wrongly no: a predicate on any other column counts as passable.
    """
    if filters is None:
        return True
    spellings = can_channel_spellings(on_read_can_ids(catalog))
    for conjunction in _disjunction(filters):
        passable = True
        for column, op, operand in conjunction:
            if column == "source":
                passable &= _scalar_predicate(CAN_SOURCE, op, operand)
            elif column == "channel":
                passable &= any(_scalar_predicate(name, op, operand) for name in spellings)
        if passable:
            return True
    return False


def filter_rows(frame: pd.DataFrame, filters: Any) -> pd.DataFrame:
    """The rows of a :func:`read_session` frame that pass pyarrow DNF
    ``filters``, in order, with :func:`read_session`'s fresh index -- what a
    read with those ``filters`` would have returned, for a reader that has
    already read a superset (``analysis.channels.shared_session_read``)."""
    out = frame.loc[_dnf_mask(frame, filters)].reset_index(drop=True)
    out.attrs = dict(frame.attrs)
    return out


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
            frame = _read_chunk(path, plan)
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


def _read_chunk(path: Path, plan: _ReadPlan) -> pd.DataFrame:
    """One chunk's rows under ``plan``'s filters and columns.

    A chunk written before a column existed has no such column, and pyarrow
    refuses a projection that names it; such a chunk is read whole and
    projected, as the whole-session read would have used it (its rows, with
    the column missing), rather than skipped as unreadable.
    """
    if plan.read_columns is None:
        return pd.read_parquet(path, filters=plan.read_filters)
    try:
        return pd.read_parquet(path, filters=plan.read_filters, columns=list(plan.read_columns))
    except ValueError:
        frame = pd.read_parquet(path, filters=plan.read_filters)
        return frame.loc[:, [c for c in plan.read_columns if c in frame.columns]]


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
    if all(column in frame.columns for column in VALUE_COLUMNS):
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
    aggregate (``imu_decimation.decimate_imu_long``) never sees half a tick.
    The edge is made by carrying each part's last, possibly incomplete, bin into the next
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
            frames.append(frame[NUMERIC_COLUMNS])
            batch, batch_rows = [], 0

    for path in paths:
        if path.stat().st_size == 0:
            continue
        try:
            frame = pd.read_parquet(path, columns=[*NUMERIC_COLUMNS, "payload"])
        except (OSError, ValueError, pd.errors.ParserError):
            try:  # a chunk written before the payload column existed
                frame = pd.read_parquet(path, columns=NUMERIC_COLUMNS)
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


def _coalesce_value_columns(frame: pd.DataFrame) -> pd.Series:
    value = frame["value_number"].astype("object")
    text_mask = frame["value_text"].notna()
    bool_mask = frame["value_bool"].notna()
    value.loc[text_mask] = frame.loc[text_mask, "value_text"]
    value.loc[bool_mask] = frame.loc[bool_mask, "value_bool"]
    return value


def _empty_long_frame(stored: Sequence[str] | None = None) -> pd.DataFrame:
    """:func:`read_session`'s columns with no rows; ``stored`` narrows the
    stored ones as its ``columns`` does (``value`` only with all three value
    columns)."""
    names = list(STORED_COLUMNS if stored is None else stored)
    columns: list[str] = [*names, "session", "session_path", "source_channel"]
    if all(column in names for column in VALUE_COLUMNS):
        columns.append("value")
    return pd.DataFrame(columns=columns)
