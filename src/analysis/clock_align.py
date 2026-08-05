"""Align a foreign log's clock to a CANary session, by cross-correlating RPM.

An UpRev CSV timestamps rows as milliseconds since log start, and the only
wall-clock anchor is the filename — written by the laptop, in local time, with
no timezone. The two clocks genuinely differ: measured offsets on 2026-07-27
were **-1.3 s and -1.6 s**, which is several samples at 41 ms and enough to
smear any cross-log comparison.

RPM is the alignment signal because it is already confirmed on both sides, so
the peak correlation validates three things at once: the offset, the RPM
binding, and that both loggers were watching the same events. Below
``MIN_ALIGNMENT_R`` none of that holds and no offset is claimed — that
threshold is what exposed the 2026-07-29 pair, whose 314 s of nominal overlap
contained zero simultaneous samples because plugging in the UpRev cable had
taken the bus away from the Pi.

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 4.1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Grid for the correlation search. Fine enough to resolve a sub-second
#: offset, coarse enough that a 41 ms log and a ~100 Hz broadcast channel land
#: in the same cells.
GRID_SECONDS = 0.1

#: Below this, the windows are not describing the same events and an "offset"
#: would be fitting noise. Real overlaps score 0.999+.
MIN_ALIGNMENT_R = 0.99

#: Search range. Clock skew between a laptop and a Pi is seconds, not minutes;
#: a wider window mostly buys spurious peaks.
MAX_OFFSET_SECONDS = 60.0


class AlignmentError(ValueError):
    """The two series cannot be aligned to the required confidence."""


@dataclass(frozen=True)
class Alignment:
    """A measured clock offset and the correlation that justifies it."""

    offset_seconds: float
    r: float
    overlapping_samples: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "clock_offset_seconds": round(self.offset_seconds, 3),
            "alignment_r": round(self.r, 5),
            "overlapping_samples": self.overlapping_samples,
        }


def _to_grid(series: pd.Series, t0: pd.Timestamp, n: int) -> np.ndarray:
    """Bucket-mean a timestamped series onto a fixed grid anchored at ``t0``."""
    index = pd.DatetimeIndex(series.index)
    offsets = ((index - t0).total_seconds() / GRID_SECONDS).to_numpy()
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    usable = (offsets >= 0) & (offsets < n) & np.isfinite(values)
    total = np.zeros(n)
    count = np.zeros(n)
    cells = offsets[usable].astype(int)
    np.add.at(total, cells, values[usable])
    np.add.at(count, cells, 1)
    return np.where(count > 0, total / np.maximum(count, 1), np.nan)


def align_clocks(
    reference: pd.Series,
    candidate: pd.Series,
    *,
    max_offset_seconds: float = MAX_OFFSET_SECONDS,
    min_r: float = MIN_ALIGNMENT_R,
    min_samples: int = 200,
) -> Alignment:
    """Offset to add to ``candidate``'s timestamps to line it up with ``reference``.

    Both series are timestamp-indexed RPM. Raises :class:`AlignmentError`
    rather than returning a weak result: a confidently-wrong offset is worse
    than none, because everything downstream silently inherits it.
    """
    if reference.empty or candidate.empty:
        raise AlignmentError("both series must be non-empty to align")

    t0 = min(pd.DatetimeIndex(reference.index).min(), pd.DatetimeIndex(candidate.index).min())
    t1 = max(pd.DatetimeIndex(reference.index).max(), pd.DatetimeIndex(candidate.index).max())
    n = int((t1 - t0).total_seconds() / GRID_SECONDS) + 2
    ref_grid = _to_grid(reference, t0, n)
    cand_grid = _to_grid(candidate, t0, n)

    steps = int(max_offset_seconds / GRID_SECONDS)
    best_r = -2.0
    best_shift = 0
    best_n = 0
    for shift in range(-steps, steps + 1):
        shifted = np.roll(cand_grid, shift)
        if shift > 0:
            shifted[:shift] = np.nan
        elif shift < 0:
            shifted[shift:] = np.nan
        both = np.isfinite(ref_grid) & np.isfinite(shifted)
        overlap = int(both.sum())
        if overlap < min_samples:
            continue
        a, b = ref_grid[both], shifted[both]
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if r > best_r:
            best_r, best_shift, best_n = r, shift, overlap

    if best_r < min_r:
        raise AlignmentError(
            f"best correlation {best_r:.4f} is below {min_r} over {best_n} overlapping "
            "samples — these windows are not describing the same events. The usual cause "
            "is that the two loggers were not actually recording simultaneously: on "
            "2026-07-29 a nominal 314 s overlap contained zero simultaneous samples "
            "because connecting the second logger took the bus away from the first. "
            "Check for real frames in the window before trusting any nominal overlap."
        )
    return Alignment(offset_seconds=best_shift * GRID_SECONDS, r=best_r, overlapping_samples=best_n)


__all__ = ["MIN_ALIGNMENT_R", "Alignment", "AlignmentError", "align_clocks"]
