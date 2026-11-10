"""The files of a recorded session: its telemetry chunks and their footers.

Chunk-level helpers that answer "does this session hold data, how many rows,
over what span" without reading any data. They live below the loader
(``src.preprocessing.loader``), which reads the rows, so that ``storage``
(session discovery, the catalog) and ``collection`` (replay) can ask these
questions without importing the pipeline (R09.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

PARQUET_PATTERN = "telemetry-*.parquet"


def has_telemetry(session_dir: Path) -> bool:
    """True when ``session_dir`` holds at least one non-empty telemetry chunk.

    The one "does this session have data?" predicate (R06.6). Scripts had
    drifted: some counted any chunk, some only non-empty ones. A directory
    exists from the moment the writer rotates into it, before its first chunk
    flushes, and a zero-byte chunk is not data.
    """
    return any(f.stat().st_size > 0 for f in session_dir.glob(PARQUET_PATTERN))


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
        except MemoryError:
            # Not a corrupt chunk: a process out of memory (or out of the
            # address space it capped itself at) must say so, not report the
            # session as holding no rows (D03.64).
            raise
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
            low, high = footer_utc(stats.min), footer_utc(stats.max)
            if low is not None and (lo is None or low < lo):
                lo = low
            if high is not None and (hi is None or high > hi):
                hi = high
    return SessionFooterSpan(first=lo, last=hi, rows=rows if seen else None)


def session_first_sample(session_dir: Path) -> pd.Timestamp | None:
    """The first stored sample's time (UTC), from the first chunk that has one.

    When a session's *data* starts, as opposed to when its directory name says
    it did. The two differ when the clock steps after the directory is
    created: ``2026-08-03T163440`` is named 6 h 37 min before its first sample
    (23:11:54Z), which put a drive on the wrong side of a flash (E01.6). Reads
    footers only, and only of the earliest non-empty chunk with timestamp
    statistics, so it is cheap enough to call per session in a loop. ``None``
    when no chunk carries a readable timestamp.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency elsewhere
        return None
    for chunk in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            if chunk.stat().st_size == 0:
                continue
            meta = pq.ParquetFile(chunk).metadata  # type: ignore[no-untyped-call, unused-ignore]
        except Exception:  # noqa: BLE001 - a corrupt/truncated chunk just doesn't count
            continue
        names = list(meta.schema.names)
        if "timestamp" not in names:
            continue
        column = names.index("timestamp")
        lows = []
        for group in range(meta.num_row_groups):
            stats = meta.row_group(group).column(column).statistics
            if stats is None or not stats.has_min_max:
                continue
            low = footer_utc(stats.min)
            if low is not None:
                lows.append(low)
        if lows:
            return min(lows)
    return None


#: (path, size, mtime_ns) -> the chunk's footer timestamp range. A written
#: chunk is never rewritten in place, so its footer answer is fixed; the key
#: still carries size and mtime so a replaced file is read again.
_CHUNK_RANGES: dict[tuple[str, int, int], tuple[pd.Timestamp, pd.Timestamp] | None] = {}


def chunk_time_range(chunk: Path) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The chunk's ``timestamp`` range from its footer, or ``None`` when unknown.

    ``None`` (no statistics, unreadable, 0 bytes) means "cannot rule it out";
    a reader pruning by time must then read the chunk.
    """
    try:
        stat = chunk.stat()
    except OSError:
        return None
    key = (str(chunk), stat.st_size, stat.st_mtime_ns)
    if key in _CHUNK_RANGES:
        return _CHUNK_RANGES[key]
    found: tuple[pd.Timestamp, pd.Timestamp] | None = None
    try:
        import pyarrow.parquet as pq

        if stat.st_size > 0:
            meta = pq.ParquetFile(chunk).metadata  # type: ignore[no-untyped-call, unused-ignore]
            names = list(meta.schema.names)
            if "timestamp" in names and meta.num_row_groups:
                column = names.index("timestamp")
                lows: list[pd.Timestamp] = []
                highs: list[pd.Timestamp] = []
                complete = True
                for group in range(meta.num_row_groups):
                    stats = meta.row_group(group).column(column).statistics
                    low = None if stats is None or not stats.has_min_max else footer_utc(stats.min)
                    high = None if stats is None or not stats.has_min_max else footer_utc(stats.max)
                    if low is None or high is None:
                        complete = False
                        break
                    lows.append(low)
                    highs.append(high)
                if complete:
                    found = (min(lows), max(highs))
    except Exception:  # noqa: BLE001 - unknown range: the reader keeps the chunk
        found = None
    _CHUNK_RANGES[key] = found
    return found


def chunks_overlapping(chunks: list[Path], lo: pd.Timestamp, hi: pd.Timestamp) -> list[Path]:
    """``chunks`` that may hold a ``timestamp`` in ``[lo, hi]``, by footer.

    Exact for a row filter on the same column: a chunk whose footer range is
    disjoint from the window contributes no row. A chunk without a readable
    range is kept.
    """
    kept: list[Path] = []
    for chunk in chunks:
        span = chunk_time_range(chunk)
        if span is None or (span[1] >= lo and span[0] <= hi):
            kept.append(chunk)
    return kept


def footer_utc(value: Any) -> pd.Timestamp | None:
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


def session_files_fingerprint(session_dir: Path) -> dict[str, Any]:
    """A session's parquet chunks by name, size and mtime, plus its resolved path.

    What a cache keyed on a session's contents (the feature-frame cache, the
    derived store) compares: any chunk added, removed or rewritten changes it.
    Moved verbatim from ``feature_cache._session_cache_entry`` (R09.6), so
    existing cache keys are unchanged.
    """
    files: list[dict[str, Any]] = []
    for file in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            stat = file.stat()
        except OSError:
            continue
        files.append(
            {
                "name": file.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return {
        "path": str(session_dir.expanduser().resolve()),
        "name": session_dir.name,
        "files": files,
    }
