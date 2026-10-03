"""Wide-open-throttle pulls found from the rpm slope alone (H07.7).

In a fixed gear, dRPM/dt is proportional to engine torque minus drag, so a
sustained rpm rise is a pull whatever the throttle channel says. Detection is
purely signal-driven: no in-drive tagging. Moved from ``tools/pull_report.py``
so ``src`` (the dyno's session reader, the workbench) can find pulls too; the
tool still prints its report from here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class RpmSeries:
    label: str
    seconds: np.ndarray  # seconds since session start
    rpm: np.ndarray
    t0: pd.Timestamp
    low_rate: bool = False
    #: P02.10: the samples are a change-only broadcast channel's stored rows,
    #: so between two of them the value *held* (an unchanged frame is not
    #: stored). Sampled as a step, never interpolated: a straight line between
    #: a held value and the next change would invent a ramp that was never on
    #: the bus.
    held: bool = False


@dataclass
class Pull:
    start_s: float
    end_s: float
    rpm_start: float
    rpm_end: float
    band_drdt: dict[int, float]
    peak_drdt: float
    dip: bool  # slope collapsed mid-pull — lift/shift contamination likely


def detect_pulls(
    series: RpmSeries,
    *,
    grid_ms: int,
    min_drdt: float,
    min_duration: float,
    min_span: float,
    band: int,
) -> list[Pull]:
    grid_s = grid_ms / 1000.0
    grid = np.arange(series.seconds[0], series.seconds[-1] + grid_s, grid_s)
    if series.held:
        at = np.clip(np.searchsorted(series.seconds, grid, side="right") - 1, 0, None)
        sampled = series.rpm[at]
    else:
        sampled = np.interp(grid, series.seconds, series.rpm)
    rpm = (
        pd.Series(sampled)
        .rolling(max(3, int(0.25 / grid_s)), center=True, min_periods=1)
        .median()
        .to_numpy()
    )
    drdt = np.gradient(rpm, grid)

    rising = drdt >= min_drdt
    # Close sub-0.3s holes so one noisy bucket doesn't split a pull.
    hole = max(1, int(0.3 / grid_s))
    closed = rising.copy()
    run_start = None
    for i, val in enumerate(rising):
        if val:
            if run_start is not None and i - run_start <= hole:
                closed[run_start:i] = True
            run_start = None
        elif run_start is None:
            run_start = i

    pulls: list[Pull] = []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], closed.astype(int), [0]))))
    for lo, hi in zip(edges[::2], edges[1::2], strict=True):
        seg_t, seg_rpm, seg_drdt = grid[lo:hi], rpm[lo:hi], drdt[lo:hi]
        if seg_t[-1] - seg_t[0] < min_duration or seg_rpm[-1] - seg_rpm[0] < min_span:
            continue
        bands: dict[int, float] = {}
        for b in np.unique((seg_rpm // band * band).astype(int)):
            sel = (seg_rpm // band * band).astype(int) == b
            bands[int(b)] = float(np.median(seg_drdt[sel]))
        med = float(np.median(seg_drdt))
        pulls.append(
            Pull(
                start_s=float(seg_t[0]),
                end_s=float(seg_t[-1]),
                rpm_start=float(seg_rpm[0]),
                rpm_end=float(seg_rpm[-1]),
                band_drdt=bands,
                peak_drdt=float(np.max(seg_drdt)),
                dip=bool(np.min(seg_drdt) < 0.4 * med),
            )
        )
    return pulls


__all__ = ["Pull", "RpmSeries", "detect_pulls"]
