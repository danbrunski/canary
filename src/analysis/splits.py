"""Independent physical drives and the grouped train/calibration/test splits (B02.3).

Moved out of :mod:`src.analysis.behavior_validation` (R05.13), which re-exports
every name here, so the fold logic sits beside :mod:`src.analysis.drives` — the
one physical-drive definition it groups by — rather than inside the 1,800-line
evaluator. Pieces of one physical drive never cross folds; drives are ordered
by parsed time, never by directory name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import pandas as pd

from src.analysis import drives as _drives
from src.control.sessions import session_started_at
from src.decode.behavior_models import DriveRecord, DriveSplitRecord
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

#: Default join gap between adjacent directories of one physical drive. A
#: capture arm/disarm rotates the writer with ~0 s gap; an engine restart
#: after a short stop is still the same outing (same fuel, ambient, heat).
DEFAULT_JOIN_GAP_SECONDS = 600.0

Fold = Literal["train", "calibration", "test"]


class UnorderedSessionsError(ValueError):
    """A session piece has neither a parseable start stamp nor timestamps."""


class LeakedDriveError(ValueError):
    """Pieces of one physical drive were assigned to more than one fold."""


SEGMENT_MARKER = _drives.SEGMENT_MARKER


def parent_session(piece: str) -> str:
    """The recording directory a (possibly ``#segment``) piece came from."""
    return _drives.parent_session(piece)


@dataclass(frozen=True)
class Drive:
    """One independent physical drive: every piece recorded during it."""

    drive_id: str
    pieces: tuple[str, ...]
    started_at: datetime
    ended_at: datetime

    def record(self) -> DriveRecord:
        return DriveRecord(
            drive_id=self.drive_id,
            pieces=list(self.pieces),
            started_at=self.started_at.isoformat(),
            ended_at=self.ended_at.isoformat(),
        )


class PieceRanges(dict[str, tuple[datetime | None, datetime | None]]):
    """``{piece: (first, last)}``, plus each row's physical drive when the
    ranges came from a frame (:func:`piece_time_ranges`).

    ``drive_rows`` (piece, epoch seconds, drive label) is what
    :func:`group_drives` groups on: the one physical-drive definition the
    trainers use (``src.analysis.drives``), captures and activity pauses
    included. A plain mapping (no rows) falls back to joining recordings by
    their ranges.
    """

    drive_rows: pd.DataFrame | None = None

    def merge(self, other: Mapping[str, tuple[datetime | None, datetime | None]]) -> None:
        """Add ``other``'s pieces, keeping both sides' drive rows."""
        self.update(other)
        rows = getattr(other, "drive_rows", None)
        if isinstance(rows, pd.DataFrame):
            self.drive_rows = (
                rows
                if self.drive_rows is None
                else pd.concat([self.drive_rows, rows], ignore_index=True)
            )


def piece_time_ranges(frame: pd.DataFrame) -> PieceRanges:
    """``{piece: (first, last)}`` from the frame's timestamps, if it has them,
    carrying the rows' physical drives for :func:`group_drives`."""
    if SESSION_COLUMN not in frame.columns:
        return PieceRanges()
    pieces = frame[SESSION_COLUMN].astype(str)
    if TIMESTAMP_COLUMN not in frame.columns:
        return PieceRanges({piece: (None, None) for piece in pieces.unique()})
    stamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True, errors="coerce")
    grouped = pd.DataFrame({"piece": pieces, "t": stamps}).groupby("piece", sort=False)["t"]
    out = PieceRanges()
    for piece, lo, hi in zip(
        grouped.min().index, grouped.min().to_numpy(), grouped.max().to_numpy(), strict=True
    ):
        first = None if pd.isna(lo) else pd.Timestamp(lo).tz_convert(UTC).to_pydatetime()
        last = None if pd.isna(hi) else pd.Timestamp(hi).tz_convert(UTC).to_pydatetime()
        out[str(piece)] = (first, last)
    if stamps.notna().any():
        keep = stamps.notna().to_numpy()
        rows = frame.loc[keep]
        out.drive_rows = pd.DataFrame(
            {
                "piece": pieces.to_numpy()[keep],
                "t": (stamps[keep] - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy(),
                "drive": _drives.row_drive_labels(
                    rows, session_column=SESSION_COLUMN, timestamp_column=TIMESTAMP_COLUMN
                ),
            }
        )
    return out


def group_drives(
    ranges: Mapping[str, tuple[datetime | None, datetime | None]],
    *,
    join_gap_seconds: float = DEFAULT_JOIN_GAP_SECONDS,
) -> list[Drive]:
    """Group pieces into independent drives, oldest first.

    Ranges from a frame (:class:`PieceRanges` with rows) group by physical
    drive, the definition every trainer uses (``src.analysis.drives``):
    vehicle activity, captures bridging, a pause longer than the join gap
    splitting even one recording. Drives are then joined when one starts
    within ``join_gap_seconds`` of the previous one's end, and pieces sharing
    a drive travel together (a piece cannot cross folds).

    A plain mapping groups by parent directory: a parent's start is its
    parsed name stamp, else its earliest timestamp; its end is its latest
    timestamp, else its start; parents join when the next starts within
    ``join_gap_seconds`` of the current group's end.

    Why that fallback exists (R05.13): ``drives.py`` says recordings are not
    drives, and for a frame that carries row timestamps they are not — the
    physical path above is taken. The fallback serves the ranges that have
    *no rows to group on*: a feature frame without a ``timestamp`` column
    (its pieces can only be ordered by their name stamps) and the
    ``inventory_behavior_inputs`` / test callers that hand over bare
    ``{piece: (first, last)}`` ranges. Refusing those would refuse to order
    sessions whose order is known; grouping them by activity is impossible.
    """
    rows = getattr(ranges, "drive_rows", None)
    if isinstance(rows, pd.DataFrame) and not rows.empty:
        return _group_physical(ranges, rows, join_gap_seconds=join_gap_seconds)
    by_parent: dict[str, list[str]] = {}
    for piece in ranges:
        by_parent.setdefault(parent_session(piece), []).append(piece)
    spans: list[_drives.SessionSpan] = []
    unordered: list[str] = []
    for parent, pieces in by_parent.items():
        firsts = [t for p in pieces if (t := ranges[p][0]) is not None]
        lasts = [t for p in pieces if (t := ranges[p][1]) is not None]
        named = session_started_at(parent)
        first = min(firsts) if firsts else None
        start = named if named is not None else first
        if start is None:
            unordered.append(parent)
            continue
        end = max([start, *lasts]) if lasts else start
        spans.append(_drives.SessionSpan(parent, start, end))
    if unordered:
        raise UnorderedSessionsError(
            "cannot order sessions without a parseable start stamp or timestamps: "
            + ", ".join(sorted(unordered))
        )
    return [
        _make_drive([(s.start, s.end, s.name) for s in group], by_parent)
        for group in _drives.join_spans(spans, gap_seconds=join_gap_seconds)
    ]


def _group_physical(
    ranges: Mapping[str, tuple[datetime | None, datetime | None]],
    rows: pd.DataFrame,
    *,
    join_gap_seconds: float,
) -> list[Drive]:
    if join_gap_seconds < _drives.DRIVE_GAP_SECONDS:
        raise ValueError(
            f"drive_join_gap_seconds {join_gap_seconds:g} is shorter than the physical-drive "
            f"gap ({_drives.DRIVE_GAP_SECONDS:g} s) the frame's drives were stamped with; "
            "it cannot split them further"
        )
    extents = rows.dropna(subset=["drive"]).groupby("drive")["t"].agg(["min", "max"])
    physical = [
        _drives.SessionSpan(str(label), _epoch(lo), _epoch(hi))
        for label, lo, hi in extents.itertuples(name=None)
    ]
    coarse = {
        span.name: index
        for index, group in enumerate(_drives.join_spans(physical, gap_seconds=join_gap_seconds))
        for span in group
    }
    labelled = rows.dropna(subset=["drive"])
    together = [
        tuple(str(p) for p in dict.fromkeys(members))
        for members in labelled.groupby(labelled["drive"].map(coarse))["piece"].agg(list).tolist()
    ]
    together.extend((str(piece),) for piece in ranges)
    first = rows.groupby("piece")["t"].min()
    last = rows.groupby("piece")["t"].max()
    drives: list[Drive] = []
    for unit in _drives.recording_units_of(together):
        pieces = sorted(unit, key=lambda p: (first.get(p, float("inf")), _segment_order(p)))
        timed = [p for p in pieces if p in first.index]
        if not timed:
            continue
        drives.append(
            Drive(
                drive_id=parent_session(pieces[0]),
                pieces=tuple(pieces),
                started_at=_epoch(float(min(first[p] for p in timed))),
                ended_at=_epoch(float(max(last[p] for p in timed))),
            )
        )
    return sorted(drives, key=lambda d: (d.started_at, d.drive_id))


def _epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def _make_drive(
    spans: Sequence[tuple[datetime, datetime, str]], by_parent: Mapping[str, list[str]]
) -> Drive:
    pieces: list[str] = []
    for _start, _end, parent in spans:
        pieces.extend(sorted(by_parent[parent], key=_segment_order))
    return Drive(
        drive_id=spans[0][2],
        pieces=tuple(pieces),
        started_at=min(start for start, _end, _parent in spans),
        ended_at=max(end for _start, end, _parent in spans),
    )


def _segment_order(piece: str) -> tuple[int, str]:
    _, _, segment = piece.partition(SEGMENT_MARKER)
    return (int(segment) if segment.isdigit() else 0, piece)


@dataclass(frozen=True)
class DriveSplit:
    train: tuple[Drive, ...]
    calibration: tuple[Drive, ...]
    test: tuple[Drive, ...]
    policy: str
    join_gap_seconds: float
    excluded: tuple[Drive, ...] = ()

    def pieces(self, fold: Fold) -> set[str]:
        drives = {"train": self.train, "calibration": self.calibration, "test": self.test}[fold]
        return {piece for drive in drives for piece in drive.pieces}

    def record(self) -> DriveSplitRecord:
        return DriveSplitRecord(
            policy=self.policy,
            join_gap_seconds=self.join_gap_seconds,
            train=[d.record() for d in self.train],
            calibration=[d.record() for d in self.calibration],
            test=[d.record() for d in self.test],
            excluded=[d.record() for d in self.excluded],
        )


def chronological_split(
    drives: Sequence[Drive],
    *,
    n_test: int,
    n_calibration: int = 0,
    join_gap_seconds: float = DEFAULT_JOIN_GAP_SECONDS,
    excluded: Sequence[Drive] = (),
) -> DriveSplit:
    """Newest ``n_test`` drives test, the ``n_calibration`` before them
    calibration, the rest train — by parsed start time."""
    ordered = sorted(drives, key=lambda d: (d.started_at, d.ended_at))
    n_test = max(0, min(n_test, len(ordered)))
    n_calibration = max(0, min(n_calibration, len(ordered) - n_test))
    cut_test = len(ordered) - n_test
    cut_cal = cut_test - n_calibration
    return DriveSplit(
        train=tuple(ordered[:cut_cal]),
        calibration=tuple(ordered[cut_cal:cut_test]),
        test=tuple(ordered[cut_test:]),
        policy=(
            f"chronological by parsed start: newest {n_test} drive(s) test, "
            f"previous {n_calibration} calibration, remainder train"
        ),
        join_gap_seconds=join_gap_seconds,
        excluded=tuple(excluded),
    )


def explicit_split(
    drives: Sequence[Drive],
    assignment: Mapping[str, Fold],
    *,
    join_gap_seconds: float = DEFAULT_JOIN_GAP_SECONDS,
) -> DriveSplit:
    """Folds named per piece or parent directory; refuses a drive in two folds."""
    folds: dict[str, list[Drive]] = {"train": [], "calibration": [], "test": []}
    excluded: list[Drive] = []
    for drive in drives:
        named = {
            assignment[key]
            for piece in drive.pieces
            for key in (piece, parent_session(piece))
            if key in assignment
        }
        if len(named) > 1:
            raise LeakedDriveError(
                f"drive {drive.drive_id} (pieces {', '.join(drive.pieces)}) is assigned to "
                f"folds {sorted(named)}; pieces of one physical drive may not cross folds"
            )
        if not named:
            excluded.append(drive)
            continue
        folds[next(iter(named))].append(drive)
    return DriveSplit(
        train=tuple(folds["train"]),
        calibration=tuple(folds["calibration"]),
        test=tuple(folds["test"]),
        policy="explicit assignment",
        join_gap_seconds=join_gap_seconds,
        excluded=tuple(excluded),
    )


def training_leaks(
    test_drives: Sequence[Drive],
    training_pieces: Iterable[str],
) -> dict[str, list[str]]:
    """Test drives that contain any piece (or parent directory) a model trained on."""
    trained = {parent_session(piece) for piece in training_pieces}
    leaks: dict[str, list[str]] = {}
    for drive in test_drives:
        hit = sorted({p for p in drive.pieces if parent_session(p) in trained})
        if hit:
            leaks[drive.drive_id] = hit
    return leaks


__all__ = [
    "DEFAULT_JOIN_GAP_SECONDS",
    "SEGMENT_MARKER",
    "Drive",
    "DriveSplit",
    "Fold",
    "LeakedDriveError",
    "PieceRanges",
    "UnorderedSessionsError",
    "chronological_split",
    "explicit_split",
    "group_drives",
    "parent_session",
    "piece_time_ranges",
    "training_leaks",
]
