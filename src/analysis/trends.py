"""Trend tests for session-fingerprint drift (Task 4.4, eval-longitudinal-and-dtc.md).

Two independent, textbook tests, run per stat over the session-ordered
fingerprint series (Task 4.1) against the frozen reference epoch (Task 4.3):

* :func:`mann_kendall` — the standard nonparametric monotonic-trend test
  (Kendall's tau / the S-statistic, normal-approximated significance, with
  the usual tie correction) over the RAW stat values. Answers "is this stat
  drifting up or down over time", independent of the reference band.
* :func:`cusum` — the classic two-sided *tabular* CUSUM drift detector
  (Montgomery, *Introduction to Statistical Quality Control*) over the
  stat's values STANDARDIZED against the reference epoch's mean/std.
  Answers "has this stat sustained a shift away from the reference epoch's
  baseline" — this is what Task 4.6 alerts on.

Both are pure numpy over a flat sequence of floats — no I/O, no session
walking, no config. :mod:`src.control.trends` builds the per-stat
session-ordered series from the on-disk fingerprint sidecars + reference
epoch and calls :func:`evaluate_trends`, the one function here that knows
about :class:`~src.analysis.reference_epoch.ReferenceEpoch`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from src.analysis.reference_epoch import ReferenceEpoch, StatSummary

MannKendallTrend = Literal["increasing", "decreasing", "no trend"]
CusumDirection = Literal["upper", "lower"]

#: Textbook tabular-CUSUM defaults (Montgomery): ``k`` (the "slack"/reference
#: value, in reference-std units) is half the shift size worth detecting;
#: ``h`` (the decision interval, same units) is the alarm threshold. 4-5
#: sigma is the usual range trading detection speed against false-alarm rate.
DEFAULT_CUSUM_K = 0.5
DEFAULT_CUSUM_H = 5.0
DEFAULT_MANN_KENDALL_ALPHA = 0.05


@dataclass(frozen=True)
class MannKendallResult:
    """One Mann-Kendall trend-test outcome."""

    n: int
    s: int
    var_s: float
    z: float
    p_value: float
    tau: float
    trend: MannKendallTrend

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "s": self.s,
            "var_s": self.var_s,
            "z": self.z,
            "p_value": self.p_value,
            "tau": self.tau,
            "trend": self.trend,
        }


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def mann_kendall(
    values: Sequence[float], *, alpha: float = DEFAULT_MANN_KENDALL_ALPHA
) -> MannKendallResult | None:
    """Standard Mann-Kendall monotonic-trend test, with tie correction.

    ``S = sum_{i<j} sign(x_j - x_i)``; ``Var(S)`` uses the standard tied-group
    correction; the normal-approximated ``z`` uses the usual continuity
    correction (``S-1``/``S+1``). Returns ``None`` when there are fewer than
    2 observations (the test is undefined). ``trend`` is ``"no trend"``
    whenever the two-tailed p-value is >= ``alpha``, regardless of the sign
    of ``z``/``tau`` — a nonzero sample slope is not by itself significant.
    """
    n = len(values)
    if n < 2:
        return None
    arr = np.asarray(values, dtype=float)
    # diffs[i, j] = x_j - x_i, so the upper triangle (i < j) is exactly the
    # S-statistic's sign(x_j - x_i) terms.
    diffs = arr[None, :] - arr[:, None]
    upper_i, upper_j = np.triu_indices(n, k=1)
    s = int(np.sum(np.sign(diffs[upper_i, upper_j])))

    _, counts = np.unique(arr, return_counts=True)
    tie_term = float(np.sum(counts * (counts - 1) * (2 * counts + 5)))
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18.0

    if var_s <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / math.sqrt(var_s)
    elif s < 0:
        z = (s + 1) / math.sqrt(var_s)
    else:
        z = 0.0

    p_value = 2.0 * (1.0 - _normal_cdf(abs(z)))
    denom = n * (n - 1) / 2.0
    tau = s / denom if denom > 0 else 0.0

    trend: MannKendallTrend
    if p_value < alpha and z > 0:
        trend = "increasing"
    elif p_value < alpha and z < 0:
        trend = "decreasing"
    else:
        trend = "no trend"

    return MannKendallResult(n=n, s=s, var_s=var_s, z=z, p_value=p_value, tau=tau, trend=trend)


@dataclass(frozen=True)
class CusumResult:
    """One two-sided tabular-CUSUM outcome — the FINAL state after walking
    the whole series (``sh``/``sl``), plus whether/where it first alarmed."""

    n: int
    k: float
    h: float
    sh: float
    sl: float
    alarm: bool
    direction: CusumDirection | None
    first_alarm_index: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "k": self.k,
            "h": self.h,
            "sh": self.sh,
            "sl": self.sl,
            "alarm": self.alarm,
            "direction": self.direction,
            "first_alarm_index": self.first_alarm_index,
        }


def cusum(
    values: Sequence[float],
    *,
    reference_mean: float,
    reference_std: float,
    k: float = DEFAULT_CUSUM_K,
    h: float = DEFAULT_CUSUM_H,
) -> CusumResult | None:
    """Classic two-sided tabular CUSUM over ``values`` standardized against
    the reference epoch's mean/std (Montgomery's ``C+``/``C-`` formulation)::

        z_i  = (x_i - reference_mean) / reference_std
        Sh_i = max(0, Sh_{i-1} + z_i - k)
        Sl_i = max(0, Sl_{i-1} - z_i - k)
        alarm when Sh_i > h  or  Sl_i > h

    Returns ``None`` when there are no observations or the reference has no
    spread (``reference_std <= 0`` — a degenerate reference, not a real
    "every session was identical" one, so no meaningful drift test is
    possible against it).
    """
    n = len(values)
    if n == 0 or reference_std <= 0:
        return None
    sh = 0.0
    sl = 0.0
    first_alarm_index: int | None = None
    for i, value in enumerate(values):
        z = (float(value) - reference_mean) / reference_std
        sh = max(0.0, sh + z - k)
        sl = max(0.0, sl - z - k)
        if first_alarm_index is None and (sh > h or sl > h):
            first_alarm_index = i
    alarm = sh > h or sl > h
    direction: CusumDirection | None = None
    if alarm:
        direction = "upper" if sh > h else "lower"
    return CusumResult(
        n=n,
        k=k,
        h=h,
        sh=sh,
        sl=sl,
        alarm=alarm,
        direction=direction,
        first_alarm_index=first_alarm_index,
    )


@dataclass(frozen=True)
class StatTrend:
    """One stat's full trend picture: latest observation, reference band,
    Mann-Kendall verdict, CUSUM state — over its (possibly ragged, Task 4.1)
    session-ordered series."""

    stat: str
    session_ids: tuple[str, ...]
    values: tuple[float, ...]
    latest_session: str
    latest_value: float
    reference_mean: float
    reference_std: float
    reference_band: tuple[float, float]
    mann_kendall: MannKendallResult | None
    cusum: CusumResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stat": self.stat,
            "n": len(self.session_ids),
            # Task 4.5's dashboard sparkline needs the actual series, not
            # just the latest point — small (one float per session with a
            # fingerprint), so no pagination/truncation here.
            "session_ids": list(self.session_ids),
            "values": list(self.values),
            "latest_session": self.latest_session,
            "latest_value": self.latest_value,
            "reference_mean": self.reference_mean,
            "reference_std": self.reference_std,
            "reference_band": list(self.reference_band),
            "mann_kendall": None if self.mann_kendall is None else self.mann_kendall.to_dict(),
            "cusum": None if self.cusum is None else self.cusum.to_dict(),
        }


def evaluate_stat_trend(
    stat: str,
    observations: Sequence[tuple[str, float]],
    reference: StatSummary,
    *,
    cusum_k: float = DEFAULT_CUSUM_K,
    cusum_h: float = DEFAULT_CUSUM_H,
    mann_kendall_alpha: float = DEFAULT_MANN_KENDALL_ALPHA,
) -> StatTrend | None:
    """Evaluate one stat's session-ordered ``[(session_id, value), ...]``
    series against its reference-epoch summary. Returns ``None`` for an
    empty series (the stat has no observations at all)."""
    if not observations:
        return None
    session_ids = tuple(session_id for session_id, _ in observations)
    values = [value for _, value in observations]
    mk = mann_kendall(values, alpha=mann_kendall_alpha)
    cs = cusum(
        values,
        reference_mean=reference.mean,
        reference_std=reference.std,
        k=cusum_k,
        h=cusum_h,
    )
    band = (
        reference.quantiles.get("p10", reference.mean - 2.0 * reference.std),
        reference.quantiles.get("p90", reference.mean + 2.0 * reference.std),
    )
    return StatTrend(
        stat=stat,
        session_ids=session_ids,
        values=tuple(values),
        latest_session=session_ids[-1],
        latest_value=values[-1],
        reference_mean=reference.mean,
        reference_std=reference.std,
        reference_band=band,
        mann_kendall=mk,
        cusum=cs,
    )


def evaluate_trends(
    fingerprints: Sequence[tuple[str, Mapping[str, float]]],
    reference: ReferenceEpoch,
    *,
    cusum_k: float = DEFAULT_CUSUM_K,
    cusum_h: float = DEFAULT_CUSUM_H,
    mann_kendall_alpha: float = DEFAULT_MANN_KENDALL_ALPHA,
) -> dict[str, StatTrend]:
    """Evaluate every stat the reference epoch knows about, over its ragged
    session-ordered series.

    ``fingerprints`` must already be session-ordered (oldest first). A
    session missing a given stat is simply excluded from THAT stat's series
    (Task 4.1's "omitted, not zeroed" contract) — it doesn't drag the trend
    toward zero or break the ordering of the sessions that do carry it.
    """
    results: dict[str, StatTrend] = {}
    for stat, summary in reference.stats.items():
        observations = [
            (session_id, stats[stat]) for session_id, stats in fingerprints if stat in stats
        ]
        trend = evaluate_stat_trend(
            stat,
            observations,
            summary,
            cusum_k=cusum_k,
            cusum_h=cusum_h,
            mann_kendall_alpha=mann_kendall_alpha,
        )
        if trend is not None:
            results[stat] = trend
    return results


__all__ = [
    "DEFAULT_CUSUM_H",
    "DEFAULT_CUSUM_K",
    "DEFAULT_MANN_KENDALL_ALPHA",
    "CusumDirection",
    "CusumResult",
    "MannKendallResult",
    "MannKendallTrend",
    "StatTrend",
    "cusum",
    "evaluate_stat_trend",
    "evaluate_trends",
    "mann_kendall",
]
