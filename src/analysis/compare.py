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

"Same instant" is itself a claim, and until 2026-09 this module did not honour
it: it averaged each side onto a one-second grid and inner-joined the bins.
Two samples per second whose values disagreed by 100 at every acquired instant
came back ``r2=1.0, slope=1.0, stable`` (roadmap task C01). Pairing now goes
through :mod:`src.analysis.pairing`: real observations on the acquisition
clock, each used at most once, within an explicit
:class:`~src.analysis.pairing.PairingPolicy` the caller must supply. The old
``grid=`` keyword is rejected by name rather than reinterpreted, because a one
second bin was never a statement about acquisition mismatch. Binned means are
still available for *pictures* — :func:`binned_summary` — and are never fed to
:func:`regress`.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.channels import TIMESTAMP_BASIS_COLUMN, read_channels
from src.analysis.pairing import (
    ComparisonRefusedError,
    InputNotCapturedError,
    MatchedSamples,
    PairingPolicy,
    TimestampBasis,
    check_clock_support,
    match_ordered,
    to_seconds,
    weakest_basis,
)
from src.paths import BINDINGS_YAML, DIDS_YAML

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
    #: Genuinely paired observations — not the count of either input series,
    #: and not a count of grid cells. Equals ``len(align_pairs(...).pairs)``.
    n_pairs: int
    r_squared: float
    #: ``b = a * slope + offset``.
    slope: float
    offset: float
    slope_drift_pct: float | None
    scale_verdict: ScaleVerdict
    #: How the pairs were formed. Part of the result because a number without
    #: its tolerance is not a finding.
    policy: PairingPolicy
    #: Which side's samples were walked as the reference.
    reference: Literal["a", "b"]
    #: Clock provenance per side (weakest row on that side).
    timestamp_basis_a: TimestampBasis
    timestamp_basis_b: TimestampBasis
    #: Observations on each side that found no partner within tolerance.
    unmatched_a: int
    unmatched_b: int
    #: Absolute acquisition-time separation over the accepted pairs, seconds.
    separation_median_s: float
    separation_p95_s: float
    separation_max_s: float
    #: Mean and standard deviation of ``b - (a * slope + offset)`` over the
    #: pairs, in ``b``'s engineering units. r² is scale-free and can read
    #: 0.99 while the residual is physically large; these say how large.
    residual_bias: float
    residual_spread: float
    #: Mean and standard deviation of ``b - a`` over the pairs — the residual
    #: of the *1:1 hypothesis*, in ``b``'s units. This is the statistic a
    #: unit-agreement claim rests on: the post-fit residual above is invariant
    #: to a wrong scale or offset (the fit absorbs them) and has zero mean by
    #: construction, so it cannot catch either. A 1.0 -> 1.05 scale edit on a
    #: coolant byte moves ``difference_bias`` by ~7 degC and the post-fit
    #: residual by nothing.
    difference_bias: float
    difference_spread: float

    @property
    def stable(self) -> bool:
        return self.scale_verdict == "stable"

    def describe(self) -> str:
        drift = "n/a" if self.slope_drift_pct is None else f"{self.slope_drift_pct:.2f}%"
        return (
            f"{self.a} -> {self.b}: r2={self.r_squared:.5f} n={self.n_pairs} "
            f"scale={self.slope:.6g} offset={self.offset:.6g} "
            f"drift={drift} [{self.scale_verdict}] "
            f"resid={self.residual_bias:+.4g}±{self.residual_spread:.4g} "
            f"b-a={self.difference_bias:+.4g}±{self.difference_spread:.4g} "
            f"sep(med/p95/max)={self.separation_median_s * 1000.0:.0f}/"
            f"{self.separation_p95_s * 1000.0:.0f}/{self.separation_max_s * 1000.0:.0f}ms "
            f"unmatched={self.unmatched_a}/{self.unmatched_b} "
            f"basis={self.timestamp_basis_a}/{self.timestamp_basis_b} "
            f"policy=({self.policy.describe()})"
        )

    def as_dict(self) -> dict[str, object]:
        """JSON-ready form; the policy is expanded rather than repr'd."""
        payload = asdict(self)
        payload["policy"] = self.policy.as_dict()
        return payload


@dataclass(frozen=True)
class AlignedPairs:
    """What :func:`align_pairs` returns: identifiable pairs plus their provenance.

    ``pairs`` has one row per accepted pair with columns ``timestamp_a``,
    ``timestamp_b``, ``a``, ``b``, ``index_a``, ``index_b`` and
    ``separation_s``. The indices are row labels of the long frame
    :func:`~src.analysis.channels.read_channels` produced for the two specs,
    so every pair can be traced back to the two rows it came from.
    """

    a: str
    b: str
    pairs: pd.DataFrame
    policy: PairingPolicy
    reference: Literal["a", "b"]
    timestamp_basis_a: TimestampBasis
    timestamp_basis_b: TimestampBasis
    n_a: int
    n_b: int
    unmatched_a: int
    unmatched_b: int

    @property
    def n_pairs(self) -> int:
        return int(len(self.pairs))

    @property
    def empty(self) -> bool:
        return self.pairs.empty


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


_GRID_MIGRATION = (
    "grid= is gone from the comparison contract (roadmap C01). A grid was a bin "
    "width, never a statement about how far apart two acquired samples may be, "
    'and binned means are not pairs. Pass policy=PairingPolicy.within("<tol>") '
    "with the acquisition tolerance you can justify; use binned_summary() if what "
    "you wanted was a picture."
)


def _refuse_grid(grid: object) -> None:
    if grid is not None:
        raise TypeError(_GRID_MIGRATION)


def _side(long: pd.DataFrame, spec: str) -> pd.DataFrame:
    return long[long["spec"] == spec]


def _refuse_if_absent(spec: str, side: pd.DataFrame) -> None:
    """ "Never captured" is not a clock problem and must not be reported as one.

    A side with no observations has no timestamp basis, and letting it fall
    through to :func:`check_clock_support` blamed ``metadata_json`` for a
    channel the profile simply did not poll — the ``delivery_error`` /
    ``tuning-did`` shape from AGENTS.md, misattributed.
    """
    if side.empty:
        raise InputNotCapturedError(
            f"{spec}: no observations in this session, so there is nothing to "
            "pair. The channel was never captured (or does not decode) — this is "
            "an unavailable input, not insufficient coverage.",
            spec=spec,
        )


def _pairs_frame(
    side_a: pd.DataFrame, side_b: pd.DataFrame, matched: MatchedSamples
) -> pd.DataFrame:
    rows_a = side_a.iloc[matched.index_a]
    rows_b = side_b.iloc[matched.index_b]
    return pd.DataFrame(
        {
            "timestamp_a": rows_a["timestamp"].to_numpy(),
            "timestamp_b": rows_b["timestamp"].to_numpy(),
            "a": rows_a["value"].to_numpy(dtype=float),
            "b": rows_b["value"].to_numpy(dtype=float),
            "index_a": rows_a.index.to_numpy(),
            "index_b": rows_b.index.to_numpy(),
            "separation_s": matched.separation_s,
        }
    )


def align_pairs(
    session: Path | pd.DataFrame,
    a: str,
    b: str,
    *,
    policy: PairingPolicy,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    grid: object = None,
) -> AlignedPairs:
    """Pair real observations of ``a`` and ``b`` on the acquisition clock.

    Each accepted pair is two rows whose acquisition times differ by at most
    ``policy.max_separation``; no row appears in two pairs, and nothing is
    averaged, forward-filled or interpolated. Sources have genuinely
    different cadences — broadcast frames near 100 Hz against DIDs at 15 Hz
    and Mode 01 PIDs near 1 Hz — so the reference side (``policy.reference``,
    ``auto`` = the sparser one) is walked in order and each of its samples
    takes its nearest unused partner. Samples with no partner are counted,
    not invented.

    Refuses (:class:`ComparisonRefusedError`) when either side's timestamp
    provenance cannot support the tolerance asked for — see
    :func:`src.analysis.pairing.check_clock_support`.
    """
    _refuse_grid(grid)
    long = read_channels(
        session, [a, b], bindings_path=bindings_path, dids_path=dids_path, acquired_time=True
    )
    side_a, side_b = _side(long, a), _side(long, b)
    _refuse_if_absent(a, side_a)
    _refuse_if_absent(b, side_b)
    times_a, times_b = to_seconds(side_a["timestamp"]), to_seconds(side_b["timestamp"])
    basis_a = weakest_basis(side_a[TIMESTAMP_BASIS_COLUMN])
    basis_b = weakest_basis(side_b[TIMESTAMP_BASIS_COLUMN])
    check_clock_support(
        policy,
        basis_a=basis_a,
        times_a=times_a,
        basis_b=basis_b,
        times_b=times_b,
        label_a=a,
        label_b=b,
    )
    matched = match_ordered(times_a, times_b, policy)
    return AlignedPairs(
        a=a,
        b=b,
        pairs=_pairs_frame(side_a, side_b, matched),
        policy=policy,
        reference=matched.reference,
        timestamp_basis_a=basis_a,
        timestamp_basis_b=basis_b,
        n_a=len(side_a),
        n_b=len(side_b),
        unmatched_a=matched.unmatched_a,
        unmatched_b=matched.unmatched_b,
    )


def binned_summary(
    session: Path | pd.DataFrame,
    a: str,
    b: str,
    *,
    grid: str = "1s",
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
) -> pd.DataFrame:
    """Per-bin means of each side, for plotting. **Not pairs. Not evidence.**

    One row per ``grid`` cell with ``a_mean``, ``b_mean`` and the sample
    counts ``n_a``/``n_b`` that went into each. A bin mean is a synthetic
    value that was never observed, and two bin means say nothing about
    whether any sample of ``a`` was acquired near any sample of ``b`` — which
    is exactly how the C01 counterexample scored r²=1 while disagreeing at
    every instant. Feed this to a chart; never to :func:`regress`.
    """
    long = read_channels(
        session, [a, b], bindings_path=bindings_path, dids_path=dids_path, acquired_time=True
    )
    columns: dict[str, pd.Series] = {}
    for label, spec in (("a", a), ("b", b)):
        part = _side(long, spec)
        series = pd.Series(
            part["value"].to_numpy(dtype=float),
            index=pd.DatetimeIndex(pd.to_datetime(part["timestamp"], utc=True)),
        ).sort_index()
        binned = series.resample(grid)
        columns[f"{label}_mean"] = binned.mean()
        columns[f"n_{label}"] = binned.count()
    out = pd.concat(columns, axis=1)
    out = out[(out["n_a"] > 0) | (out["n_b"] > 0)]
    out.index.name = "bin"
    return out.reset_index()


def _refuse_if_constant(spec: str, values: np.ndarray, n_pairs: int) -> None:
    span = _relative_span(values)
    if span < MIN_RELATIVE_SPAN:
        raise ComparisonRefusedError(
            f"{spec} is effectively constant over these {n_pairs} pairs "
            f"(relative span {span:.2e}). Any scale fits a constant, so a fit "
            "here would describe the session, not the signals — compare over a "
            "window where it moves."
        )


def _separation_stats(separation_s: np.ndarray) -> tuple[float, float, float]:
    return (
        float(np.median(separation_s)),
        float(np.percentile(separation_s, 95)),
        float(np.max(separation_s)),
    )


def compare_channels(
    session: Path | pd.DataFrame,
    a: str,
    b: str,
    *,
    policy: PairingPolicy,
    min_pairs: int = MIN_PAIRS,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    grid: object = None,
) -> ChannelComparison:
    """Compare two channels at the same instants. Refuses when it cannot.

    ``a`` and ``b`` are :mod:`src.analysis.channels` specs, so both arrive in
    engineering units and ``slope``/``offset`` are directly the mapping
    between them. ``policy`` states how far apart two "simultaneous"
    observations may have been acquired; there is no default because that
    tolerance is part of the claim.

    Raises :class:`ComparisonRefusedError` when the clock cannot carry the
    tolerance, when the pairing is too sparse, or when either side is
    effectively constant — the situations in which comparing aggregates
    yields a confident-looking number and comparing values yields nothing.
    """
    _refuse_grid(grid)
    aligned = align_pairs(
        session, a, b, policy=policy, bindings_path=bindings_path, dids_path=dids_path
    )
    if aligned.n_pairs < min_pairs:
        raise ComparisonRefusedError(
            f"{a} vs {b}: only {aligned.n_pairs} aligned pairs within "
            f"{policy.max_separation_s * 1000.0:g} ms (need {min_pairs}; "
            f"{aligned.n_a} and {aligned.n_b} observations, "
            f"{aligned.unmatched_a}/{aligned.unmatched_b} unmatched). The series "
            "may not overlap in time, or the tolerance may be tighter than the "
            "slower channel's cadence can meet."
        )
    x = aligned.pairs["a"].to_numpy(dtype=float)
    y = aligned.pairs["b"].to_numpy(dtype=float)
    _refuse_if_constant(a, x, aligned.n_pairs)
    _refuse_if_constant(b, y, aligned.n_pairs)
    fit = regress(x, y)
    if fit is None:
        raise ComparisonRefusedError(
            f"{a} vs {b}: no identifiable relationship in {aligned.n_pairs} pairs"
        )
    drift_pct, verdict = scale_stability(x, y)
    residual = y - (x * fit.slope + fit.offset)
    difference = y - x
    median_s, p95_s, max_s = _separation_stats(aligned.pairs["separation_s"].to_numpy())
    return ChannelComparison(
        a=a,
        b=b,
        n_pairs=aligned.n_pairs,
        r_squared=fit.r_squared,
        slope=fit.slope,
        offset=fit.offset,
        slope_drift_pct=drift_pct,
        scale_verdict=verdict,
        policy=policy,
        reference=aligned.reference,
        timestamp_basis_a=aligned.timestamp_basis_a,
        timestamp_basis_b=aligned.timestamp_basis_b,
        unmatched_a=aligned.unmatched_a,
        unmatched_b=aligned.unmatched_b,
        separation_median_s=median_s,
        separation_p95_s=p95_s,
        separation_max_s=max_s,
        residual_bias=float(np.mean(residual)),
        residual_spread=float(np.std(residual)),
        difference_bias=float(np.mean(difference)),
        difference_spread=float(np.std(difference)),
    )


__all__ = [
    "DRIFT_SIGNIFICANCE_SIGMA",
    "MIN_PAIRS",
    "MIN_RELATIVE_SPAN",
    "STABILITY_WINDOWS",
    "STABLE_DRIFT_PCT",
    "AlignedPairs",
    "ChannelComparison",
    "ComparisonRefusedError",
    "Fit",
    "InputNotCapturedError",
    "PairingPolicy",
    "ScaleVerdict",
    "align_pairs",
    "binned_summary",
    "compare_channels",
    "regress",
    "scale_stability",
]
