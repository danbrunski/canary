"""The one sanctioned way to compare two channels.

Every wrong finding this project has had to retract came from the same move:
comparing two signals by their *aggregates* instead of their values at the
same instant.

* ``ECM_CALC_LOAD_B`` (2026-05) was bound to a byte because its per-window
  **mean** matched another byte's mean in every window. That byte is the low
  byte of a 16-bit rpm counter — it averages near mid-scale whatever the
  engine is doing, so it matches anything else near mid-scale. Retracted
  2026-08-06.
* ``achieved_hz_capture`` (2026-08) reported a rate as **count / wall-clock**.
  A capture polling at a solid 14.93 Hz read as 11.25 Hz because the window
  opened late, and a 97.7 Hz broadcast read as 52.9 Hz because the engine was
  off for half the session.
* The Mode 22 "session cycling" diagnosis read **cumulative** counters as if
  they were scoped to the capture.
* The 2026-08-04 fuel finding averaged delivery error across rpm bands whose
  *composition* differed between the two sides.

Four incidents, one error. Each was caught only after it had been acted on.
The defence is structural rather than remembered: this module is the only
comparison entry point, it aligns raw samples itself, and it will not accept
a pre-aggregated frame because it does not take one — you hand it *specs* and
a session, and it does the pairing.

It also refuses the conditions that make an aggregate comparison look
convincing when it is not: too few genuinely paired samples, a side that
barely moves (where any scale fits), and stale pairings. A refusal names
which one, because "no result" and "no relationship" are different answers.

Ranking by r² alone is not enough either — over a single drive nearly every
engine signal correlates with rpm. :attr:`ChannelComparison.scale_verdict`
carries the sub-window stability check that distinguishes a real binding from
mere co-variation, and separates *unstable* from *unresolvable*.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.channels import read_channels

#: Sub-windows used for the scale-stability check. Three is the fewest that
#: can distinguish "drifting" from "one noisy window".
STABILITY_WINDOWS = 3

#: Slope spread (max-min over mean, percent) below which a scale is treated as
#: holding across the session.
STABLE_DRIFT_PCT = 1.5

#: Slope spread must exceed this many pooled standard errors before it counts
#: as evidence of real drift rather than fitting noise. The conventional 3σ —
#: the test requires *evidence* of instability rather than a cutoff tuned
#: until the answers look right.
DRIFT_SIGNIFICANCE_SIGMA = 3.0

#: Below this many aligned pairs, neither r² nor the drift figure means
#: anything and :func:`compare_channels` refuses rather than reporting a
#: number that will be quoted.
MIN_PAIRS = 30

#: A side whose values span less than this fraction of their own magnitude is
#: effectively constant. Any scale fits a constant, which is precisely how a
#: mean-to-mean match passes for a binding.
MIN_RELATIVE_SPAN = 1e-3

#: Whether a fitted scale held across the session.
#:
#: ``unresolved`` is not a soft ``drifting``: it means this session lacked the
#: dynamic range to settle the question, and a session that exercises the
#: signal is needed before anything is promoted on the strength of it.
ScaleVerdict = Literal["stable", "unresolved", "drifting"]


class ComparisonRefusedError(ValueError):
    """The data cannot support a comparison, as distinct from showing none.

    Raised rather than returned because the refusal cases all produce a
    *plausible-looking* number if you compute one anyway.
    """


@dataclass(frozen=True)
class Fit:
    """One OLS pass of ``y = x * slope + offset``, with the slope's own error."""

    r_squared: float
    slope: float
    offset: float
    slope_se: float


@dataclass(frozen=True)
class ChannelComparison:
    """Two channels compared at the same instants, never by aggregate."""

    a: str
    b: str
    #: Genuinely paired samples — not the count of either input series.
    n_pairs: int
    r_squared: float
    #: ``b = a * slope + offset``.
    slope: float
    offset: float
    slope_drift_pct: float | None
    scale_verdict: ScaleVerdict

    @property
    def stable(self) -> bool:
        return self.scale_verdict == "stable"

    def describe(self) -> str:
        drift = "n/a" if self.slope_drift_pct is None else f"{self.slope_drift_pct:.2f}%"
        return (
            f"{self.a} -> {self.b}: r2={self.r_squared:.5f} n={self.n_pairs} "
            f"scale={self.slope:.6g} offset={self.offset:.6g} "
            f"drift={drift} [{self.scale_verdict}]"
        )


def regress(x: np.ndarray, y: np.ndarray) -> Fit | None:
    """OLS with the slope standard error, or ``None`` if unidentifiable."""
    if len(x) < 3:
        return None
    x_var = float(np.var(x))
    if x_var <= 0.0 or float(np.var(y)) <= 0.0:
        # A constant side has no relationship to find, only one to invent.
        return None
    slope, offset = np.polyfit(x, y, 1)
    predicted = x * slope + offset
    residual = float(np.sum((y - predicted) ** 2))
    total = float(np.sum((y - float(np.mean(y))) ** 2))
    if total <= 0.0:
        return None
    dof = len(x) - 2
    slope_se = float(np.sqrt((residual / dof) / (x_var * len(x)))) if dof > 0 else float("inf")
    return Fit(
        r_squared=1.0 - residual / total,
        slope=float(slope),
        offset=float(offset),
        slope_se=slope_se,
    )


def scale_stability(x: np.ndarray, y: np.ndarray) -> tuple[float | None, ScaleVerdict]:
    """Does the fitted scale hold across disjoint sub-windows?

    A large spread has two very different causes, and only one of them
    condemns a binding:

    * a signal that merely co-varies with the reference over one drive — its
      scale really does differ per window, by more than the fits' own
      uncertainty;
    * a slow, near-monotonic signal that barely moves inside any one window,
      so each slope is fitted mostly to noise. ``0x551`` byte 0 *is* the
      confirmed coolant byte and still spread 83% on the 2026-08-06 drive.

    So ``drifting`` requires the spread to be both practically large and
    larger than sampling noise explains. Otherwise the answer is
    ``unresolved``: this session cannot settle it.
    """
    if len(x) < STABILITY_WINDOWS * 3:
        return None, "unresolved"
    fits: list[Fit] = []
    for chunk_x, chunk_y in zip(
        np.array_split(x, STABILITY_WINDOWS),
        np.array_split(y, STABILITY_WINDOWS),
        strict=True,
    ):
        fit = regress(chunk_x, chunk_y)
        if fit is None:
            return None, "unresolved"
        fits.append(fit)
    slopes = [fit.slope for fit in fits]
    mean_slope = statistics.fmean(slopes)
    if mean_slope == 0.0:
        return None, "unresolved"
    spread = abs(max(slopes) - min(slopes))
    drift_pct = spread / abs(mean_slope) * 100.0
    if drift_pct <= STABLE_DRIFT_PCT:
        return drift_pct, "stable"
    pooled_se = float(np.sqrt(statistics.fmean([fit.slope_se**2 for fit in fits])))
    if not np.isfinite(pooled_se) or pooled_se <= 0.0:
        return drift_pct, "unresolved"
    return drift_pct, (
        "drifting" if spread > DRIFT_SIGNIFICANCE_SIGMA * pooled_se else "unresolved"
    )


def _relative_span(values: np.ndarray) -> float:
    scale = max(abs(float(np.mean(values))), float(np.std(values)), 1e-12)
    return float(np.ptp(values)) / scale


def align_pairs(
    session: Path | pd.DataFrame,
    a: str,
    b: str,
    *,
    grid: str = "1s",
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
) -> pd.DataFrame:
    """Both channels on one clock, as a two-column frame of paired samples.

    Binned onto ``grid`` and inner-joined, so a bin with no sample on one side
    is dropped rather than carrying a neighbour's value forward. Sources have
    genuinely different cadences — broadcast frames near 100 Hz against DIDs
    at 15 Hz and Mode 01 PIDs near 1 Hz — and forward-filling across that gap
    is the stale-join trap that makes unrelated series look synchronised.
    """
    frame = read_channels(session, [a, b], bindings_path=bindings_path, dids_path=dids_path)
    columns: dict[str, pd.Series] = {}
    for spec in (a, b):
        part = frame[frame["spec"] == spec]
        series = pd.Series(
            part["value"].to_numpy(dtype=float),
            index=pd.to_datetime(part["timestamp"], utc=True),
        ).sort_index()
        columns[spec] = series.resample(grid).mean()
    joined = pd.concat({"a": columns[a], "b": columns[b]}, axis=1, join="inner").dropna()
    return joined


def compare_channels(
    session: Path | pd.DataFrame,
    a: str,
    b: str,
    *,
    grid: str = "1s",
    min_pairs: int = MIN_PAIRS,
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
) -> ChannelComparison:
    """Compare two channels at the same instants. Refuses when it cannot.

    ``a`` and ``b`` are :mod:`src.analysis.channels` specs, so both arrive in
    engineering units and ``slope``/``offset`` are directly the mapping
    between them.

    Raises :class:`ComparisonRefusedError` when the pairing is too sparse or either
    side is effectively constant — the two situations in which comparing
    aggregates yields a confident-looking number and comparing values yields
    nothing at all.
    """
    joined = align_pairs(session, a, b, grid=grid, bindings_path=bindings_path, dids_path=dids_path)
    if len(joined) < min_pairs:
        raise ComparisonRefusedError(
            f"{a} vs {b}: only {len(joined)} aligned pairs at grid={grid!r} "
            f"(need {min_pairs}). The series may not overlap in time, or the "
            "grid may be finer than the slower channel's cadence."
        )
    x = joined["a"].to_numpy(dtype=float)
    y = joined["b"].to_numpy(dtype=float)
    for spec, values in ((a, x), (b, y)):
        if _relative_span(values) < MIN_RELATIVE_SPAN:
            raise ComparisonRefusedError(
                f"{spec} is effectively constant over these {len(joined)} pairs "
                f"(relative span {_relative_span(values):.2e}). Any scale fits a "
                "constant, so a fit here would describe the session, not the "
                "signals — compare over a window where it moves."
            )
    fit = regress(x, y)
    if fit is None:
        raise ComparisonRefusedError(
            f"{a} vs {b}: no identifiable relationship in {len(joined)} pairs"
        )
    drift_pct, verdict = scale_stability(x, y)
    return ChannelComparison(
        a=a,
        b=b,
        n_pairs=len(joined),
        r_squared=fit.r_squared,
        slope=fit.slope,
        offset=fit.offset,
        slope_drift_pct=drift_pct,
        scale_verdict=verdict,
    )


__all__ = [
    "DRIFT_SIGNIFICANCE_SIGMA",
    "MIN_PAIRS",
    "MIN_RELATIVE_SPAN",
    "STABILITY_WINDOWS",
    "STABLE_DRIFT_PCT",
    "ChannelComparison",
    "ComparisonRefusedError",
    "Fit",
    "ScaleVerdict",
    "align_pairs",
    "compare_channels",
    "regress",
    "scale_stability",
]
