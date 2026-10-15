"""GPS altitude noise, measured where the road contributes nothing: parked (H07.4).

The squat fit's flat-ground gate needs every grade standard error to come
from the receiver's real wander, not from an independence assumption. This
module models that wander (:class:`AltitudeNoise`) and measures it from a
stationary stretch of the same session (:func:`measure_altitude_noise`).

Depends only on :mod:`src.analysis.dyno_contract`. Moved verbatim out of
``dyno.py`` (R16).
"""

from __future__ import annotations

import math

import numpy as np
from pydantic import Field, model_validator

from src.analysis.dyno_contract import DynoRefusal, FloatArray, MotionSamples, StrictModel


class AltitudeNoise(StrictModel):
    """GPS altitude noise, measured where the road contributes nothing: parked.

    GNSS altitude is not white. On this truck, parked, it wanders by
    2.5-5 m (sd) with an autocorrelation still 0.6-0.97 after 20 s
    (``~/canary-data/sessions-0920``; ``tests/fixtures/gnss_altitude``), so a
    grade standard error that assumes independent samples is 3-10x too small,
    and a flat road reads as a crest. From altitude alone, road shape and
    receiver wander cannot be told apart at the scale of a segment; a
    stationary stretch of the same session measures the wander by itself.

    Modelled as white noise plus a powered-exponential wander, with variogram
    ``γ(h) = white·[h > 0] + correlated·(1 − exp(−(h/τ)^α))``, 0 < α < 2 (a
    valid variogram throughout). α = 1 is an AR(1)/Ornstein-Uhlenbeck wander;
    real parked altitude here is smoother, γ growing as ~h^1.5 from 1 to 30 s,
    which an exponential (linear at short lags) misdescribes at the lags a
    segment spans. Every
    standard error in :func:`fit_squat` is ``−wᵀ·Γ·w`` for weights ``w`` that
    sum to zero (a slope), which needs only the variogram, never the sill, so
    a receiver that has not stopped wandering within the reference is still
    described at the lags a segment spans.
    """

    white_var_m2: float = Field(ge=0.0)
    correlated_var_m2: float = Field(ge=0.0)
    tau_s: float = Field(gt=0.0)
    #: Shape of the wander's variogram at lags below ``tau_s``.
    alpha: float = Field(default=1.0, gt=0.0, lt=2.0)
    #: Length of the stationary reference (s) and its sample count.
    reference_s: float = Field(gt=0.0)
    n_reference: int = Field(ge=2)

    @model_validator(mode="after")
    def _some_noise(self) -> AltitudeNoise:
        if self.white_var_m2 + self.correlated_var_m2 <= 0.0:
            raise ValueError("an altitude noise model with zero variance resolves nothing")
        return self

    def variogram(self, lag_s: FloatArray) -> FloatArray:
        h = np.abs(np.asarray(lag_s, dtype=np.float64))
        gamma: FloatArray = self.white_var_m2 * (h > 0) + self.correlated_var_m2 * (
            1.0 - np.exp(-((h / self.tau_s) ** self.alpha))
        )
        return gamma


#: A stationary reference for :func:`measure_altitude_noise`: at least this
#: long, and never faster than this (GNSS speed at rest jitters ~0.1 m/s).
#: The segment se it yields is unbiased (median model/true 0.96-1.03 for white,
#: τ = 2, 10 and 40 s noise); its spread shrinks with length: 10th-90th
#: percentile 0.84-1.10 at 120 s, 0.93-1.09 at 300 s. Park for 3 minutes.
ALTITUDE_NOISE_MIN_REFERENCE_S = 180.0
ALTITUDE_NOISE_MAX_SPEED_MPS = 0.5
_ALTITUDE_NOISE_ALPHAS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 1.9)


def measure_altitude_noise(
    samples: MotionSamples,
    *,
    min_duration_s: float = ALTITUDE_NOISE_MIN_REFERENCE_S,
    max_speed_mps: float = ALTITUDE_NOISE_MAX_SPEED_MPS,
) -> AltitudeNoise | DynoRefusal:
    """Fit :class:`AltitudeNoise` to a stationary stretch of the same session.

    The empirical variogram is taken on a uniform grid at the reference's
    median sample interval, at ~40 log-spaced lags up to half its length, and
    fitted (relative least squares, non-negative variances) over a grid of
    ``τ`` from half a sample to 20x the reference and of ``α`` from 0.5 to 1.9.
    """
    name = "altitude_noise"
    if samples.altitude_m is None:
        return DynoRefusal(name, "inputs_missing", "GPS altitude was not captured")
    moving = float(np.max(np.abs(samples.speed_mps)))
    if moving > max_speed_mps:
        return DynoRefusal(
            name,
            "not_stationary",
            f"speed reaches {moving:.2f} m/s; the noise reference must be parked "
            f"(<= {max_speed_mps:.1f} m/s) so the road contributes nothing",
        )
    duration = float(samples.t_s[-1] - samples.t_s[0])
    if duration < min_duration_s:
        return DynoRefusal(
            name,
            "too_few_samples",
            f"{duration:.1f} s parked; the noise reference needs {min_duration_s:.0f} s",
        )
    step = float(np.median(np.diff(samples.t_s)))
    grid = np.arange(samples.t_s[0], samples.t_s[-1] + 1e-9, step)
    z = np.interp(grid, samples.t_s, samples.altitude_m)
    lags = np.unique(np.round(np.logspace(0.0, math.log10(len(z) / 2), 40)).astype(int))
    lags = lags[(lags >= 1) & (lags < len(z) - 1)]
    gamma = np.array([0.5 * float(np.mean((z[k:] - z[:-k]) ** 2)) for k in lags])
    if not np.any(gamma > 0.0):
        return DynoRefusal(
            name,
            "no_spread",
            "GPS altitude did not change at all over the reference (frozen or quantised); "
            "it cannot say how precisely altitude resolves grade",
        )
    h = lags * step
    weight = 1.0 / np.maximum(gamma, float(gamma[gamma > 0].min()))
    best: tuple[float, float, float, float, float] | None = None
    taus = np.logspace(math.log10(step / 2.0), math.log10(20.0 * duration), 100)
    for tau, alpha in ((t, a) for t in taus for a in _ALTITUDE_NOISE_ALPHAS):
        shape = 1.0 - np.exp(-((h / tau) ** alpha))
        candidates: list[tuple[float, float]] = []
        design = np.column_stack([np.ones_like(h), shape]) * weight[:, None]
        coef, *_ = np.linalg.lstsq(design, gamma * weight, rcond=None)
        if coef[0] >= 0.0 and coef[1] >= 0.0:
            candidates.append((float(coef[0]), float(coef[1])))
        for column in (0, 1):
            x = design[:, column]
            value = max(float(x @ (gamma * weight)) / float(x @ x), 0.0)
            candidates.append((value, 0.0) if column == 0 else (0.0, value))
        for white, corr in candidates:
            model = white + corr * shape
            sse = float(np.sum(((model - gamma) * weight) ** 2))
            if best is None or sse < best[0]:
                best = (sse, white, corr, float(tau), float(alpha))
    assert best is not None
    _, white, corr, tau, alpha = best
    return AltitudeNoise(
        white_var_m2=white,
        correlated_var_m2=corr,
        tau_s=tau,
        alpha=alpha,
        reference_s=duration,
        n_reference=len(samples),
    )
