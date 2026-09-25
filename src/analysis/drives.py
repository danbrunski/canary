"""Physical drives: the one definition every holdout, floor and count uses.

A *physical drive* is a stretch of vehicle activity: the rows the vehicle's
own modules produced (:data:`ACTIVITY_SOURCES` — ECU responses and decoded
engine broadcast), grouped so that activity no more than ``gap_seconds``
apart is one drive. Three rules, each learned from the 2026-09-21..23 corpus:

* **Recordings are not drives.** A drive is recorded in several directories
  (a daemon restart, a capture window rotating the writer), and one directory
  can hold more than one drive. The 11 h parked recording
  ``2026-09-23T032755`` kept writing GPS fixes and bus chatter all night, so
  "no data" never happened in it; only the *vehicle activity* stopped. Split
  on activity, not on data.
* **Every recording beside the ones given counts as evidence.** A training
  pool never holds captures (``discover_sessions`` skips ``_`` directories),
  yet a capture is recorded in the middle of a drive: without it the live
  sessions either side of a 20-minute capture read as two drives, and a
  retrain put one physical drive on both the validation and the training
  side. Sibling recordings bridge the grouping and never join a result.
* **Frames carry the answer.** A trainer sees a feature frame, not the
  directories. :func:`stamp_physical_drives` labels each row with its drive
  (``build_feature_frame`` does it), and every frame-level count
  (:func:`count_drives`, :func:`piece_units`) reads that label, so no
  per-caller regrouping from row timestamps can forget the captures.

Frames without the label (synthetic ones in tests) fall back to grouping
their own rows, split at gaps, through the same joiner.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.control.sessions import is_session_dir_name, session_started_at
from src.session import format_session_timestamp

log = logging.getLogger(__name__)

#: Activity no more than this far apart is one physical drive. A daemon
#: restart or a capture window leaves a gap of seconds; a key-off between two
#: drives leaves minutes.
DRIVE_GAP_SECONDS = 300.0

#: Telemetry sources that only exist while the vehicle's modules are awake:
#: OBD/UDS responses (J1979, DIDs, memory reads) and decoded engine
#: broadcast. GPS, IMU, derived channels and raw bus frames keep arriving
#: from a parked truck with the Pi powered, so they are not evidence of a
#: drive.
ACTIVITY_SOURCES: tuple[str, ...] = ("obd", "decoded")

#: Frame column :func:`stamp_physical_drives` writes: the row's drive id, or
#: ``None`` for a row no vehicle activity lies near.
DRIVE_COLUMN = "physical_drive"

#: The resampler's name for a piece of a recording split at a data gap.
SEGMENT_MARKER = "#segment-"

#: How far before the earliest given recording a sibling may start and still
#: bridge: no capture window runs this long.
_BRIDGE_LOOKBACK = timedelta(hours=12)


@dataclass(frozen=True)
class SessionSpan:
    """A stretch of one recording, first to last timestamp (UTC)."""

    name: str
    start: datetime
    end: datetime
    path: Path | None = None


@dataclass(frozen=True)
class PhysicalDrive:
    """One drive: every activity span in it, and which of them were asked about."""

    drive_id: str
    start: datetime
    end: datetime
    #: Every span that joined the drive, including sibling recordings.
    spans: tuple[SessionSpan, ...]
    #: Names of the given recordings with activity in this drive.
    sessions: tuple[str, ...]


def parent_session(name: str) -> str:
    """The recording directory a (possibly ``#segment``) piece came from."""
    return str(name).split(SEGMENT_MARKER, 1)[0]


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def drive_id_for(start: datetime) -> str:
    return f"drive-{format_session_timestamp(_utc(start))}"


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------


def join_spans(
    spans: Sequence[SessionSpan], *, gap_seconds: float = DRIVE_GAP_SECONDS
) -> list[list[SessionSpan]]:
    """Group spans into drives, oldest first: a span that overlaps the running
    group, or starts within ``gap_seconds`` of its end, joins it. Ordered by
    parsed start, never by name."""
    ordered = sorted(spans, key=lambda span: (_utc(span.start), span.name))
    groups: list[list[SessionSpan]] = []
    group_end: datetime | None = None
    gap = timedelta(seconds=gap_seconds)
    for span in ordered:
        if groups and group_end is not None and _utc(span.start) <= group_end + gap:
            groups[-1].append(span)
            group_end = max(group_end, _utc(span.end))
        else:
            groups.append([span])
            group_end = _utc(span.end)
    return groups


def split_seconds(
    seconds: np.ndarray[Any, Any], *, gap_seconds: float
) -> list[tuple[float, float]]:
    """``(first, last)`` runs of sorted epoch seconds with no step over ``gap_seconds``."""
    if seconds.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(seconds) > gap_seconds)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [seconds.size - 1]))
    return [(float(seconds[a]), float(seconds[b])) for a, b in zip(starts, ends, strict=True)]


def _from_epoch(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


# ---------------------------------------------------------------------------
# Recordings on disk
# ---------------------------------------------------------------------------

_ACTIVITY_CACHE: dict[tuple[str, int, int, int], np.ndarray[Any, Any]] = {}


def _recording_fingerprint(path: Path) -> tuple[str, int, int, int] | None:
    try:
        chunks = [c for c in path.glob("telemetry-*.parquet") if c.stat().st_size > 0]
    except OSError:
        return None
    if not chunks:
        return None
    stats = [c.stat() for c in chunks]
    return (
        str(path.resolve()),
        len(chunks),
        sum(s.st_size for s in stats),
        max(s.st_mtime_ns for s in stats),
    )


def activity_seconds(path: Path) -> np.ndarray[Any, Any]:
    """Sorted unique epoch seconds at which ``path`` recorded vehicle activity.

    The best vehicle evidence the recording holds: rows whose ``source`` is in
    :data:`ACTIVITY_SOURCES`; failing any, its raw bus frames (``can`` — a
    CAN-only collection, or a session from before decoded rows were
    persisted); failing those, nothing (GPS or IMU alone is the Pi, not the
    vehicle). A parquet with no ``source`` column (synthetic data) counts
    every row. Cached per process on the chunk files' count, size and newest
    mtime.
    """
    key = _recording_fingerprint(Path(path))
    if key is None:
        return np.empty(0, dtype="float64")
    cached = _ACTIVITY_CACHE.get(key)
    if cached is not None:
        return cached
    chunks = [c for c in sorted(Path(path).glob("telemetry-*.parquet")) if c.stat().st_size > 0]
    seconds = _read_seconds(chunks, ACTIVITY_SOURCES)
    if seconds is None:  # no ``source`` column: synthetic data, every row counts
        seconds = _read_seconds(chunks, None)
    elif not seconds.size:
        seconds = _read_seconds(chunks, ("can",))
    result = np.unique(seconds) if seconds is not None and seconds.size else np.empty(0, "float64")
    _ACTIVITY_CACHE[key] = result
    return result


def _read_seconds(
    chunks: Sequence[Path], sources: Sequence[str] | None
) -> np.ndarray[Any, Any] | None:
    """Floored epoch seconds of the rows from ``sources`` (every row when
    ``None``). ``None`` when a filter was asked for but the files carry no
    ``source`` column. Unreadable chunks are skipped, like the loader does."""
    import pyarrow as pa
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq

    if not chunks:
        return np.empty(0, dtype="float64")
    try:
        names = pq.ParquetFile(chunks[0]).schema_arrow.names  # type: ignore[no-untyped-call, unused-ignore]
    except Exception:  # noqa: BLE001
        names = []
    if sources is not None and "source" not in names:
        return None
    expression = (
        None if sources is None else ds.field("source").isin(pa.array(list(sources)))  # type: ignore[attr-defined, no-untyped-call, unused-ignore]
    )
    tables: list[Any] = []
    try:
        tables.append(
            ds.dataset([str(c) for c in chunks], format="parquet").to_table(  # type: ignore[no-untyped-call, unused-ignore]
                columns=["timestamp"], filter=expression
            )
        )
    except Exception:  # noqa: BLE001 - a torn chunk: read the rest one by one
        for chunk in chunks:
            try:
                tables.append(
                    ds.dataset(str(chunk), format="parquet").to_table(  # type: ignore[no-untyped-call, unused-ignore]
                        columns=["timestamp"], filter=expression
                    )
                )
            except Exception:  # noqa: BLE001
                continue
    parts = []
    for table in tables:
        if table.num_rows:
            seconds = _epoch_seconds(table["timestamp"].to_pandas())
            parts.append(np.floor(seconds[np.isfinite(seconds)]))
    return np.concatenate(parts) if parts else np.empty(0, dtype="float64")


def activity_spans(path: Path, *, gap_seconds: float = DRIVE_GAP_SECONDS) -> list[SessionSpan]:
    """``path``'s vehicle activity as spans, split wherever it pauses for more
    than ``gap_seconds`` (one directory can hold two drives)."""
    path = Path(path)
    return [
        SessionSpan(path.name, _from_epoch(lo), _from_epoch(hi), path)
        for lo, hi in split_seconds(activity_seconds(path), gap_seconds=gap_seconds)
    ]


def _sibling_recordings(given: Sequence[Path]) -> list[Path]:
    """Recordings beside ``given`` (live sessions and captures, never
    ``_archive`` or ``_replay-*``) that start in the window they could bridge."""
    starts = [s for p in given if (s := session_started_at(p.name)) is not None]
    if not starts:
        return []
    window_start = min(_utc(s) for s in starts) - _BRIDGE_LOOKBACK
    window_end = max(_utc(s) for s in starts) + _BRIDGE_LOOKBACK
    resolved = {p.resolve() for p in given}
    siblings: list[Path] = []
    for parent in sorted({p.parent for p in given}):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir() or not is_session_dir_name(child.name):
                continue
            if child.resolve() in resolved:
                continue
            started = session_started_at(child.name)
            if started is not None and window_start <= _utc(started) <= window_end:
                siblings.append(child)
    return siblings


def physical_drives(
    recordings: Iterable[Path], *, gap_seconds: float = DRIVE_GAP_SECONDS
) -> list[PhysicalDrive]:
    """The physical drives the given recordings took part in, oldest first.

    Sibling recordings in the same directories (captures included) join the
    grouping as evidence and are named in no drive's ``sessions``. A drive's
    id comes from its first activity, siblings included, so it is the same
    whichever subset of recordings asked.
    """
    given = [Path(p) for p in recordings]
    given_names = {p.name for p in given}
    spans: list[SessionSpan] = []
    for path in given:
        found = activity_spans(path, gap_seconds=gap_seconds)
        if not found and (started := session_started_at(path.name)) is not None:
            # No vehicle activity recorded (empty, or parked chatter only): the
            # recording is placed at its name's start, so a restart stub still
            # joins its drive, but its own data never stretches one.
            found = [SessionSpan(path.name, _utc(started), _utc(started), path)]
        spans.extend(found)
    for path in _sibling_recordings(given):
        spans.extend(activity_spans(path, gap_seconds=gap_seconds))
    drives: list[PhysicalDrive] = []
    for group in join_spans(spans, gap_seconds=gap_seconds):
        members = tuple(dict.fromkeys(s.name for s in group if s.name in given_names))
        if not members:
            continue
        start = min(_utc(s.start) for s in group)
        drives.append(
            PhysicalDrive(
                drive_id=drive_id_for(start),
                start=start,
                end=max(_utc(s.end) for s in group),
                spans=tuple(group),
                sessions=members,
            )
        )
    return drives


def recording_units(drives: Sequence[PhysicalDrive]) -> list[tuple[str, ...]]:
    """Recordings that must travel together: a directory with activity in two
    drives links them, because a directory cannot be split across a holdout."""
    return _components([d.sessions for d in drives])


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def stamp_physical_drives(
    frame: pd.DataFrame,
    recordings: Sequence[Path],
    *,
    session_column: str = "session",
    timestamp_column: str = "timestamp",
    gap_seconds: float = DRIVE_GAP_SECONDS,
) -> pd.DataFrame:
    """``frame`` with :data:`DRIVE_COLUMN`: each row's physical drive.

    A row belongs to the drive of the nearest activity span of its own
    recording, if one lies within ``gap_seconds``; otherwise it is ``None``
    (a parked row, say). Rows of recordings not among ``recordings`` are
    ``None`` too.
    """
    labels = np.full(len(frame), None, dtype=object)
    if len(frame) and session_column in frame.columns and timestamp_column in frame.columns:
        intervals: dict[str, list[tuple[float, float, str]]] = {}
        for drive in physical_drives(recordings, gap_seconds=gap_seconds):
            for span in drive.spans:
                intervals.setdefault(span.name, []).append(
                    (_utc(span.start).timestamp(), _utc(span.end).timestamp(), drive.drive_id)
                )
        parents = frame[session_column].astype(str).map(parent_session).to_numpy()
        seconds = _epoch_seconds(frame[timestamp_column])
        for parent in np.unique(parents):
            known = intervals.get(str(parent))
            if not known:
                continue
            rows = np.flatnonzero(parents == parent)
            t = seconds[rows]
            best = np.full(rows.size, np.inf)
            chosen = np.full(rows.size, None, dtype=object)
            for lo, hi, drive_id in known:
                distance = np.maximum(np.maximum(lo - t, t - hi), 0.0)
                closer = distance < best
                best[closer] = distance[closer]
                chosen[closer] = drive_id
            chosen[~(best <= gap_seconds)] = None
            labels[rows] = chosen
    return frame.assign(**{DRIVE_COLUMN: labels})


def _epoch_seconds(stamps: pd.Series) -> np.ndarray[Any, Any]:
    """Epoch seconds (float, NaN where unparseable), whatever the stored unit."""
    parsed = pd.to_datetime(pd.Series(stamps), utc=True, errors="coerce")
    return (parsed - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy(dtype="float64")


def row_drive_labels(
    frame: pd.DataFrame,
    *,
    session_column: str = "session",
    timestamp_column: str = "timestamp",
    gap_seconds: float = DRIVE_GAP_SECONDS,
) -> np.ndarray[Any, Any]:
    """Each row's physical drive.

    A stamped frame (:func:`stamp_physical_drives`) answers for itself: a row
    no vehicle activity lies near stays ``None`` and belongs to no drive (a
    parked row is not a drive). A frame nobody stamped (synthetic, in tests)
    is grouped from its own timestamps: each piece split at gaps over
    ``gap_seconds``, joined across pieces by :func:`join_spans`, labelled
    ``rows-<start>``. It has no sibling recordings to bridge with.
    """
    if DRIVE_COLUMN in frame.columns:
        stamped = frame[DRIVE_COLUMN]
        values: np.ndarray[Any, Any] = stamped.astype(object).to_numpy().copy()
        values[stamped.isna().to_numpy()] = None
        return values
    labels = np.full(len(frame), None, dtype=object)
    missing = np.ones(len(frame), dtype=bool)
    pieces = frame[session_column].astype(str).to_numpy()
    seconds = _epoch_seconds(frame[timestamp_column])
    spans: list[SessionSpan] = []
    index: dict[str, tuple[np.ndarray[Any, Any], np.ndarray[Any, Any]]] = {}
    for piece in np.unique(pieces[missing]):
        rows = np.flatnonzero(missing & (pieces == piece))
        order = rows[np.argsort(seconds[rows], kind="stable")]
        t = seconds[order]
        valid = np.isfinite(t)
        order, t = order[valid], t[valid]
        index[str(piece)] = (order, t)
        for lo, hi in split_seconds(t, gap_seconds=gap_seconds):
            spans.append(SessionSpan(str(piece), _from_epoch(lo), _from_epoch(hi)))
    for group in join_spans(spans, gap_seconds=gap_seconds):
        label = "rows-" + drive_id_for(min(_utc(s.start) for s in group)).removeprefix("drive-")
        for span in group:
            order, t = index[span.name]
            lo, hi = _utc(span.start).timestamp(), _utc(span.end).timestamp()
            inside = (t >= lo) & (t <= hi)
            labels[order[inside]] = label
    return labels


def drop_rows_without_drive(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """``frame`` without the rows no vehicle activity lies near, and how many
    were dropped.

    Such rows are parked time: the Pi recording GPS drift and bus chatter
    with the key off. The r2 sequence candidate (2026-09-23) validated on
    exactly that, a parked tail the segmenter called ``city`` from GPS drift.
    A frame nobody stamped is returned whole: it has no activity evidence to
    judge by.
    """
    if DRIVE_COLUMN not in frame.columns:
        return frame, 0
    keep = frame[DRIVE_COLUMN].notna().to_numpy()
    dropped = int((~keep).sum())
    return (frame.loc[keep].reset_index(drop=True) if dropped else frame), dropped


def count_drives(
    frame: pd.DataFrame,
    *,
    session_column: str = "session",
    timestamp_column: str = "timestamp",
    gap_seconds: float = DRIVE_GAP_SECONDS,
) -> int:
    """Independent physical drives among ``frame``'s rows."""
    if frame.empty:
        return 0
    labels = row_drive_labels(
        frame,
        session_column=session_column,
        timestamp_column=timestamp_column,
        gap_seconds=gap_seconds,
    )
    return len({label for label in labels if label is not None})


def piece_units(
    frame: pd.DataFrame,
    *,
    session_column: str = "session",
    timestamp_column: str = "timestamp",
    gap_seconds: float = DRIVE_GAP_SECONDS,
) -> list[tuple[str, ...]]:
    """Frame pieces that must be held out together, oldest first.

    Pieces sharing a physical drive are one unit, and a piece with rows in two
    drives links them (a piece cannot be split across a holdout).
    """
    if frame.empty:
        return []
    labels = row_drive_labels(
        frame,
        session_column=session_column,
        timestamp_column=timestamp_column,
        gap_seconds=gap_seconds,
    )
    pieces = frame[session_column].astype(str).to_numpy()
    seconds = _epoch_seconds(frame[timestamp_column])
    table = pd.DataFrame({"piece": pieces, "drive": labels, "t": seconds})
    first = table.groupby("piece")["t"].min()
    by_drive = table.dropna(subset=["drive"]).groupby("drive")["piece"].unique()
    groups = [tuple(str(p) for p in members) for members in by_drive.tolist()]
    groups.extend((str(piece),) for piece in first.index)
    units = _components(groups)
    return sorted(
        (tuple(sorted(unit, key=lambda p: (first.get(p, np.inf), p))) for unit in units),
        key=lambda unit: (first.get(unit[0], np.inf), unit[0]),
    )


def recording_units_of(groups: Iterable[Sequence[str]]) -> list[tuple[str, ...]]:
    """Union of named groups that share a member (pieces that must travel
    together), members in first-seen order."""
    return _components(groups)


def _components(groups: Iterable[Sequence[str]]) -> list[tuple[str, ...]]:
    """Union of groups that share a member, members in first-seen order."""
    parent: dict[str, str] = {}
    order: list[str] = []

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for group in groups:
        members = list(group)
        for member in members:
            if member not in parent:
                parent[member] = member
                order.append(member)
        for member in members[1:]:
            a, b = find(members[0]), find(member)
            if a != b:
                parent[b] = a
    out: dict[str, list[str]] = {}
    for member in order:
        out.setdefault(find(member), []).append(member)
    return [tuple(members) for members in out.values()]


__all__ = [
    "ACTIVITY_SOURCES",
    "DRIVE_COLUMN",
    "DRIVE_GAP_SECONDS",
    "SEGMENT_MARKER",
    "PhysicalDrive",
    "SessionSpan",
    "activity_seconds",
    "activity_spans",
    "count_drives",
    "drop_rows_without_drive",
    "drive_id_for",
    "join_spans",
    "parent_session",
    "physical_drives",
    "piece_units",
    "recording_units",
    "recording_units_of",
    "row_drive_labels",
    "split_seconds",
    "stamp_physical_drives",
]
