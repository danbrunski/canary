"""Derived session frames (P01): reduce first, then build the wide frame.

The offline chain every trainer and evaluator runs was *decode → wide pivot at
raw resolution → quality gate → resample*. The raw pivot is the expensive
part: one row per distinct timestamp of a ~100 Hz bus, about 1.5 KB a row,
10-15 GB for one long drive (the A02.11 baseline was OOM-killed on
``2026-09-20T152758``, 2026-09-25). The resampler only ever reads a few of
those rows, so this module keeps only those, channel by channel, straight
from the decoded long rows, and builds the wide frame from what is left.

**What the resampler reads** (``src.preprocessing.resampler``), for each grid
bin ``[t, t + interval)`` and each channel:

- the bin's last observation (``resample().last()``), forward-filled;
- for ``__gap``, the newest observation at or before ``t``: an earlier bin's
  last, or bin ``t``'s first when it sits exactly on ``t``;
- for ``__arrived``, an observation in ``(t - interval, t]``: bin
  ``t - interval``'s last when it is later than its start, or bin ``t``'s
  first when it is exactly ``t``;
- and, over every channel at once, the first and last *row* of the bin: they
  fix the grid's first and last points, and every gap longer than a bin (the
  ``session_gap_seconds`` segment split) runs from one bin's last row to the
  next occupied bin's first.

So per channel and bin it is enough to keep the first and last cell of each
kind of "observed": any row at all (for the grid), a non-null value, and one
``pd.to_numeric`` can parse (the arrival flag's view), each under the quality
gate. A kept cell carries its value, so keeping more than the extremes never
changes an answer; keeping fewer would.

**Two whole-session facts are carried, not guessed.** ``to_wide`` casts a
column to float32 only when *all* its non-null cells in the session are
numbers, and the quality gate compares a float32 cell differently from its
float64 original (150.000001 passes a 150.0 limit as float32 and fails it as
float64). A channel's cells are therefore kept for both readings, its
"non-numeric somewhere" verdict is accumulated across the session, and the
choice is made when the wide frame is built -- where ``coerce_numeric_columns``
and ``apply_quality_gate`` themselves run on the kept cells.

The reduced cells are the stored form (P01.2), so a frame read back from disk
and a freshly reduced one go through the same builder.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.fsm import PHYSICAL_LIMITS
from src.decode.bindings import Bindings
from src.preprocessing.loader import (
    INDEX_COLUMNS,
    coerce_numeric_columns,
    non_numeric_columns,
)
from src.preprocessing.quality_gate import apply_quality_gate
from src.preprocessing.resampler import (
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    ResampleConfig,
    arrived_column,
    gap_column,
    resample_wide,
)

log = logging.getLogger(__name__)

__all__ = [
    "CELL_COLUMNS",
    "ReducedSession",
    "bin_extremes",
    "grid_interval_ns",
    "reduce_long_frames",
    "reduced_channels",
    "resample_reduced",
]

#: Columns of :attr:`ReducedSession.cells`, the stored form. ``vtype`` says
#: which value column holds the cell's value (the long frame's ``value`` is an
#: ``object`` column mixing floats, text and bools, which parquet cannot hold
#: in one column): 0 null, 1 float, 2 text, 3 bool, 4 integer.
CELL_COLUMNS = (
    "channel",
    "timestamp_ns",
    "structural",
    "vtype",
    "vnum",
    "vtext",
    "vbool",
)
_NULL, _FLOAT, _TEXT, _BOOL, _INT = 0, 1, 2, 3, 4

#: Channels resampled together when the wide frame is built. The union of kept
#: timestamps over *every* channel is several hundred thousand rows on a long
#: drive; per batch it is a fraction, and the grid is the same for each batch
#: because every batch carries the bins' first and last rows.
_RESAMPLE_CHANNEL_BATCH = 24

_DAY_NS = 86_400 * 1_000_000_000


def grid_interval_ns(config: ResampleConfig) -> int:
    """The grid interval in nanoseconds, refusing one that does not tile a day.

    Bins are floored on the epoch, which is the resampler's ``start_day``
    origin only when the interval divides a day. Any rate the recipe could
    plausibly use does (1, 2, 5 Hz, 0.5 Hz...); a rate that does not would
    put reduced bins across resample bins, so it is refused rather than
    approximated.
    """
    interval_ns = int(config.interval.value)
    if interval_ns <= 0 or _DAY_NS % interval_ns:
        raise ValueError(
            f"resample interval {config.interval} does not divide a day; the reduced "
            "session path bins on the epoch and would disagree with the resampler"
        )
    if config.session_gap_seconds * 1e9 < interval_ns:
        # The segment split reads every gap between consecutive rows. Only
        # gaps longer than a bin survive the reduction (a bin keeps its first
        # and last row), so a shorter session gap could split inside a bin
        # (fuzzed at 0.25 Hz / 1 s gap: 26 of 30 frames differed).
        raise ValueError(
            f"session_gap_seconds {config.session_gap_seconds} is shorter than the "
            f"{config.interval} grid interval; the reduced session path keeps only "
            "gaps longer than one bin, so it could not reproduce the segment split"
        )
    return interval_ns


def bin_extremes(bins: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Positions of the first and last ``True`` of ``mask`` within each bin.

    ``bins`` must be non-decreasing (the cells are time-sorted).
    """
    positions = np.flatnonzero(mask)
    if positions.size == 0:
        return positions
    binned = bins[positions]
    change = binned[1:] != binned[:-1]
    first = positions[np.concatenate(([True], change))]
    last = positions[np.concatenate((change, [True]))]
    return np.union1d(first, last)


@dataclass
class ReducedSession:
    """One session, reduced to the cells its resampled frame depends on.

    ``cells`` has :data:`CELL_COLUMNS`, sorted by channel then time.
    ``non_numeric`` names channels with a non-numeric cell somewhere in the
    session (they stay ``object`` in the wide frame). ``rejects_object`` /
    ``rejects_float`` are each gate-limited channel's whole-session reject
    counts under the two dtypes (reporting only). ``timestamp_unit`` is the
    long frame's timestamp resolution, which the resampled grid inherits.
    """

    session: str
    interval_ns: int
    cells: pd.DataFrame
    non_numeric: frozenset[str]
    timestamp_unit: str
    rejects_object: dict[str, int] = field(default_factory=dict)
    rejects_float: dict[str, int] = field(default_factory=dict)

    @property
    def channels(self) -> list[str]:
        return sorted(self.cells["channel"].unique().tolist())

    def reject_counts(self) -> dict[str, int]:
        """Each channel's reject count under the dtype the session gives it."""
        out: dict[str, int] = {}
        for channel in sorted(self.rejects_object.keys() | self.rejects_float.keys()):
            source = self.rejects_object if channel in self.non_numeric else self.rejects_float
            if source.get(channel):
                out[channel] = source[channel]
        return out


def reduced_channels(session: ReducedSession) -> list[str]:
    """The value columns :func:`resample_reduced` builds, in wide-frame order."""
    return session.channels


# ---------------------------------------------------------------------------
# reduction
# ---------------------------------------------------------------------------


@dataclass
class _PartReduction:
    cells: pd.DataFrame
    non_numeric: set[str]
    rejects_object: dict[str, int]
    rejects_float: dict[str, int]
    timestamp_unit: str


def reduce_long_frames(
    session: str,
    parts: Iterable[pd.DataFrame],
    interval_ns: int,
    *,
    limits: Mapping[str, Any] = PHYSICAL_LIMITS,
    value_column: str = "value",
) -> ReducedSession:
    """Reduce a session's *decoded* long frames, given as time-disjoint parts.

    Each part must hold every row of its timestamps (``iter_session_parts``
    guarantees it) and be in the decoder's order (``_decode_long_frame``
    sorts it), because the pivot's ``aggfunc="last"`` picks, per timestamp and
    channel, the last non-null value in that order.
    """
    pieces: list[pd.DataFrame] = []
    non_numeric: set[str] = set()
    rejects_object: dict[str, int] = {}
    rejects_float: dict[str, int] = {}
    units: list[str] = []
    for long in parts:
        part = _reduce_part(long, interval_ns, limits, value_column)
        if part is None:
            continue
        pieces.append(part.cells)
        non_numeric |= part.non_numeric
        _add_counts(rejects_object, part.rejects_object)
        _add_counts(rejects_float, part.rejects_float)
        units.append(part.timestamp_unit)
    if pieces:
        cells = pd.concat(pieces, ignore_index=True)
        cells = cells.sort_values(["channel", "timestamp_ns"], kind="stable")
        cells = cells.reset_index(drop=True)
    else:
        cells = _empty_cells()
    return ReducedSession(
        session=session,
        interval_ns=interval_ns,
        cells=cells,
        non_numeric=frozenset(non_numeric),
        timestamp_unit=_finest_unit(units),
        rejects_object=rejects_object,
        rejects_float=rejects_float,
    )


def _reduce_part(
    long: pd.DataFrame, interval_ns: int, limits: Mapping[str, Any], value_column: str
) -> _PartReduction | None:
    if long.empty:
        return None
    stamps = pd.DatetimeIndex(long[TIMESTAMP_COLUMN])
    unit = str(stamps.unit)
    ts = stamps.tz_convert("UTC").as_unit("ns").tz_localize(None).to_numpy().view(np.int64)
    codes, names = pd.factorize(long["source_channel"].astype(str), sort=False)
    values = long[value_column].to_numpy(dtype=object)

    # Cells: one per (channel, timestamp), the last non-null value in the
    # decoder's order (the pivot's aggfunc="last"), or null when none is.
    order = np.lexsort((np.arange(len(long)), ts, codes))
    c, t, v = codes[order], ts[order], values[order]
    starts = np.concatenate(([True], (c[1:] != c[:-1]) | (t[1:] != t[:-1])))
    group = np.cumsum(starts) - 1
    cell_value = np.full(int(group[-1]) + 1, np.nan, dtype=object)
    present = np.flatnonzero(~pd.isna(v))
    if present.size:
        g = group[present]
        last = present[np.concatenate((g[1:] != g[:-1], [True]))]
        cell_value[group[last]] = v[last]
    cell_code = c[starts]
    cell_ts = t[starts]
    bins = cell_ts // interval_ns

    # A float column has one kind of "observed": every non-null cell parses as
    # a number and stays non-null as float32, so the three masks are one mask,
    # nothing is non-numeric, and a channel with no limit has nothing to gate.
    # The ``value_number`` chain (P02.1: every digest, fingerprint and
    # classification) is exactly that case; asking pandas per channel and part
    # cost it 2.4x its whole-session wall time.
    float_cells = ~pd.isna(cell_value) if pd.api.types.is_float_dtype(long[value_column]) else None

    keep = np.zeros(len(cell_code), dtype=bool)
    structural = np.zeros(len(cell_code), dtype=bool)
    non_numeric: set[str] = set()
    rejects_object: dict[str, int] = {}
    rejects_float: dict[str, int] = {}
    bounds = np.flatnonzero(np.concatenate(([True], cell_code[1:] != cell_code[:-1], [True])))
    for lo, hi in zip(bounds[:-1], bounds[1:], strict=True):
        name = str(names[cell_code[lo]])
        channel_bins = bins[lo:hi]
        rows = _extremes(channel_bins, np.ones(hi - lo, dtype=bool))
        structural[lo + rows] = True
        keep[lo + rows] = True
        if float_cells is not None and name not in limits:
            keep[lo + _extremes(channel_bins, float_cells[lo:hi])] = True
            continue
        column = pd.Series(cell_value[lo:hi], dtype=object)
        masks, part_non_numeric, counts = _observed_masks(name, column, limits)
        if part_non_numeric:
            non_numeric.add(name)
        for kind, count in counts.items():
            _add_counts(rejects_object if kind == "object" else rejects_float, {name: count})
        for mask in masks:
            keep[lo + _extremes(channel_bins, mask)] = True

    kept = np.flatnonzero(keep)
    cells = _cells_frame(
        names.to_numpy(dtype=object)[cell_code[kept]],
        cell_ts[kept],
        structural[kept],
        cell_value[kept],
    )
    return _PartReduction(cells, non_numeric, rejects_object, rejects_float, unit)


def _extremes(bins: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return bin_extremes(bins, mask)


def _observed_masks(
    name: str, column: pd.Series, limits: Mapping[str, Any]
) -> tuple[list[np.ndarray], bool, dict[str, int]]:
    """Every "observed" mask the resampler could read for this channel.

    Returns the masks, whether this part has a non-numeric cell (the
    ``to_wide`` verdict, computed by its own code on the part's cells), and
    the gate's reject counts per dtype.
    """
    frame = pd.DataFrame({SESSION_COLUMN: "", TIMESTAMP_COLUMN: 0, name: column})
    part_non_numeric = bool(non_numeric_columns(frame))
    counts: dict[str, int] = {}
    # As an object column (the session keeps it object if any part is
    # non-numeric): the value/gap view is "not null", the arrival view is
    # "parses as a number", both after the gate.
    gated, quality = apply_quality_gate(frame, limits=_limit_for(name, limits), log_rejects=False)
    if quality.reject_counts:
        counts["object"] = quality.total_rejected
    as_object = gated[name]
    masks = [
        as_object.notna().to_numpy(),
        pd.to_numeric(as_object, errors="coerce").notna().to_numpy(),
    ]
    if not part_non_numeric:
        # As the float32 column the session makes it if no part is non-numeric.
        as_float = coerce_numeric_columns(frame.copy())
        gated_float, quality_float = apply_quality_gate(
            as_float, limits=_limit_for(name, limits), log_rejects=False
        )
        if quality_float.reject_counts:
            counts["float"] = quality_float.total_rejected
        masks.append(gated_float[name].notna().to_numpy())
    return masks, part_non_numeric, counts


def _limit_for(name: str, limits: Mapping[str, Any]) -> Mapping[str, Any]:
    return {name: limits[name]} if name in limits else {}


def _cells_frame(
    channels: np.ndarray, ts: np.ndarray, structural: np.ndarray, values: np.ndarray
) -> pd.DataFrame:
    n = len(values)
    vtype = np.zeros(n, dtype=np.int8)
    vnum = np.full(n, np.nan, dtype=np.float64)
    vtext = np.full(n, None, dtype=object)
    vbool = np.zeros(n, dtype=bool)
    for i, value in enumerate(values):
        if value is None or (isinstance(value, float) and value != value):
            continue
        if isinstance(value, (bool, np.bool_)):
            vtype[i] = _BOOL
            vbool[i] = bool(value)
        elif isinstance(value, (int, np.integer)):
            vtype[i] = _INT
            vnum[i] = float(value)
        elif isinstance(value, (float, np.floating)):
            vtype[i] = _FLOAT
            vnum[i] = float(value)
        elif isinstance(value, str):
            vtype[i] = _TEXT
            vtext[i] = value
        else:
            raise TypeError(
                f"channel {channels[i]!r}: a {type(value).__name__} cell value has no stored form"
            )
    return pd.DataFrame(
        {
            "channel": pd.Series(channels, dtype=object),
            "timestamp_ns": ts.astype(np.int64),
            "structural": structural,
            "vtype": vtype,
            "vnum": vnum,
            "vtext": pd.Series(vtext, dtype=object),
            "vbool": vbool,
        }
    )


def _empty_cells() -> pd.DataFrame:
    return _cells_frame(
        np.array([], dtype=object),
        np.array([], dtype=np.int64),
        np.array([], dtype=bool),
        np.array([], dtype=object),
    )


def _add_counts(into: dict[str, int], counts: Mapping[str, int]) -> None:
    for key, count in counts.items():
        into[key] = into.get(key, 0) + int(count)


_UNIT_ORDER = {"s": 0, "ms": 1, "us": 2, "ns": 3}


def _as_unit(unit: str) -> Literal["s", "ms", "us", "ns"]:
    if unit == "s":
        return "s"
    if unit == "ms":
        return "ms"
    if unit == "us":
        return "us"
    return "ns"


def _finest_unit(units: Sequence[str]) -> str:
    """The unit ``pd.concat`` would give the parts' timestamps together."""
    if not units:
        return "us"
    return max(units, key=lambda unit: _UNIT_ORDER.get(unit, 3))


# ---------------------------------------------------------------------------
# rebuilding the resampled frame
# ---------------------------------------------------------------------------


def resample_reduced(
    reduced: ReducedSession,
    resample_config: ResampleConfig,
    *,
    channels: Collection[str] | None = None,
) -> pd.DataFrame:
    """The resampled frame of ``reduced``: what ``resample_long_session`` returns.

    ``channels`` projects the frame to those value columns (and their flags);
    the grid is the whole session's either way, because the bins' first and
    last rows come from every channel. A channel the session never had is
    refused by name rather than returned all-NaN.
    """
    interval_ns = grid_interval_ns(resample_config)
    if interval_ns != reduced.interval_ns:
        raise ValueError(
            f"{reduced.session}: reduced on a {reduced.interval_ns} ns grid, asked to "
            f"resample on {interval_ns} ns; re-derive the session for this rate"
        )
    all_channels = reduced.channels
    if channels is None:
        wanted = all_channels
    else:
        missing = sorted(set(channels) - set(all_channels))
        if missing:
            raise KeyError(f"{reduced.session}: no such channel(s) in the session: {missing}")
        wanted = [name for name in all_channels if name in set(channels)]

    cells = reduced.cells
    structural_ts = _structural_timestamps(cells, interval_ns)
    if len(structural_ts) == 0:
        return resample_wide(
            pd.DataFrame(columns=[*INDEX_COLUMNS, *wanted]), config=resample_config
        )
    by_channel: dict[str, pd.DataFrame] = {
        str(name): rows for name, rows in cells.groupby("channel", sort=True)
    }
    counts = reduced.reject_counts()
    if counts:
        log.info(
            "quality gate rejected cells in session",
            extra={"session": reduced.session, "reject_counts": counts},
        )

    values: list[pd.DataFrame] = []
    gaps: list[pd.DataFrame] = []
    arrivals: list[pd.DataFrame] = []
    head: pd.DataFrame | None = None
    batches = [
        wanted[i : i + _RESAMPLE_CHANNEL_BATCH]
        for i in range(0, len(wanted), _RESAMPLE_CHANNEL_BATCH)
    ] or [[]]
    for batch in batches:
        wide = _wide_frame(reduced, batch, by_channel, structural_ts)
        # The session's counts were logged above; these are kept cells only.
        gated, _ = apply_quality_gate(wide, log_rejects=False)
        resampled = resample_wide(gated, config=resample_config)
        if head is None:
            head = resampled[[SESSION_COLUMN, TIMESTAMP_COLUMN]]
        values.append(resampled[batch])
        gaps.append(resampled[[gap_column(name) for name in batch]])
        arrivals.append(resampled[[arrived_column(name) for name in batch]])
    assert head is not None
    return pd.concat([head, *values, *gaps, *arrivals], axis=1)


def _structural_timestamps(cells: pd.DataFrame, interval_ns: int) -> np.ndarray:
    """Each occupied bin's first and last row time, over every channel."""
    stamps = cells.loc[cells["structural"].to_numpy(dtype=bool), "timestamp_ns"].to_numpy()
    if stamps.size == 0:
        return np.asarray(stamps, dtype=np.int64)
    bins = stamps // interval_ns
    frame = pd.DataFrame({"bin": bins, "ts": stamps})
    grouped = frame.groupby("bin", sort=True)["ts"]
    bounds = np.union1d(grouped.min().to_numpy(), grouped.max().to_numpy())
    return np.asarray(bounds, dtype=np.int64)


def _wide_frame(
    reduced: ReducedSession,
    batch: Sequence[str],
    by_channel: Mapping[str, pd.DataFrame],
    structural_ts: np.ndarray,
) -> pd.DataFrame:
    """The wide frame ``to_wide`` would build, cut to the kept timestamps."""
    parts = [structural_ts]
    parts.extend(by_channel[name]["timestamp_ns"].to_numpy() for name in batch)
    stamps = np.unique(np.concatenate(parts)) if parts else structural_ts
    columns: dict[str, Any] = {
        SESSION_COLUMN: pd.Series([reduced.session] * len(stamps), dtype=object),
        TIMESTAMP_COLUMN: pd.to_datetime(stamps, utc=True).as_unit(
            _as_unit(reduced.timestamp_unit)
        ),
    }
    for name in batch:
        rows = by_channel[name]
        positions = np.searchsorted(stamps, rows["timestamp_ns"].to_numpy())
        cells = _cell_objects(rows)
        column = np.full(len(stamps), np.nan, dtype=object)
        column[positions] = cells
        columns[name] = pd.Series(column, dtype=object)
    wide = pd.DataFrame(columns)
    # to_wide's cast, with the session-wide verdict: only a channel carrying a
    # non-numeric cell somewhere stays object.
    return coerce_numeric_columns(wide, keep_object=reduced.non_numeric & set(batch))


def _cell_objects(rows: pd.DataFrame) -> np.ndarray:
    """The long frame's ``value`` objects back from the typed columns."""
    vtype = rows["vtype"].to_numpy()
    out = np.full(len(rows), np.nan, dtype=object)
    vnum = rows["vnum"].to_numpy()
    for kind, convert in ((_FLOAT, float), (_INT, int)):
        idx = np.flatnonzero(vtype == kind)
        if idx.size:
            out[idx] = [convert(x) for x in vnum[idx]]
    idx = np.flatnonzero(vtype == _TEXT)
    if idx.size:
        out[idx] = rows["vtext"].to_numpy(dtype=object)[idx]
    idx = np.flatnonzero(vtype == _BOOL)
    if idx.size:
        out[idx] = [bool(x) for x in rows["vbool"].to_numpy()[idx]]
    return out


def resample_numeric_reduced(
    reduced: ReducedSession, resample_config: ResampleConfig
) -> pd.DataFrame:
    """``resample_long`` of a session reduced from its ``value_number`` rows.

    The chain ``loader.resample_and_segment(value="value_number")`` runs (the
    digest, fingerprints, classification): no decode, no quality gate, IMU
    decimated per tick before the reduction (:func:`reduce_numeric_session`).
    The kept cells are turned back into long rows and handed to the real
    ``resample_long``: per channel it reads each bin's last value and the
    extremes the flags need, and its grid runs from a segment's first to last
    numeric reading, which are bin extremes; its segment split reads every
    row's time, which the structural cells keep.
    """
    from src.preprocessing.resampler import resample_long

    grid_interval_ns(resample_config)
    cells = reduced.cells
    value = cells["vnum"].where(cells["vtype"].isin([_FLOAT, _INT]))
    long = pd.DataFrame(
        {
            SESSION_COLUMN: pd.Series([reduced.session] * len(cells), dtype=object),
            TIMESTAMP_COLUMN: pd.to_datetime(cells["timestamp_ns"].to_numpy(), utc=True).as_unit(
                _as_unit(reduced.timestamp_unit)
            ),
            "source_channel": cells["channel"].to_numpy(dtype=object),
            "value_number": value.to_numpy(dtype=np.float64),
        }
    )
    long = long.sort_values(TIMESTAMP_COLUMN, kind="stable").reset_index(drop=True)
    return resample_long(long, config=resample_config)


def reduce_numeric_session(
    session_dir: Path,
    resample_config: ResampleConfig,
    *,
    part_rows: int | None = None,
    on_read: Bindings | None | Literal["current"] = "current",
) -> ReducedSession:
    """Reduce a session for :func:`resample_numeric_reduced` (P01.2).

    Reads ``value_number`` part by part on tick-aligned boundaries, decimates
    raw IMU acceleration per tick exactly as ``resample_and_segment`` does, and
    keeps the cells ``resample_long`` reads. No gate, and no decode beyond the
    loader's decode on read of the ``decoded:`` rows (P02.8; ``on_read`` is
    that catalog, ``loader.iter_session_numeric_parts``): that chain has
    neither.
    """
    from src.preprocessing.loader import (
        SESSION_PART_ROWS,
        _decimate_imu_long,
        iter_session_numeric_parts,
    )

    interval_ns = grid_interval_ns(resample_config)
    parts = (
        _decimate_imu_long(part, hz=resample_config.hz)
        for part in iter_session_numeric_parts(
            session_dir,
            target_rows=part_rows or SESSION_PART_ROWS,
            align_ns=interval_ns,
            on_read=on_read,
        )
    )
    return reduce_long_frames(
        session_dir.name, parts, interval_ns, limits={}, value_column="value_number"
    )
