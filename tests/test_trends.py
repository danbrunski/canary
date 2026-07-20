"""Tests for ``src.analysis.trends`` (Task 4.4, eval-longitudinal-and-dtc.md).

Mann-Kendall and CUSUM are checked against hand-derived / independently
recomputed reference values (not just "doesn't crash"), plus synthetic
drifting vs. stationary series to prove each test actually distinguishes
the two.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from src.analysis.reference_epoch import ReferenceEpoch, StatSummary
from src.analysis.trends import (
    cusum,
    evaluate_stat_trend,
    evaluate_trends,
    mann_kendall,
)

# --------------------------------------------------------------------------- #
# Mann-Kendall
# --------------------------------------------------------------------------- #


def test_mann_kendall_none_for_fewer_than_two_observations() -> None:
    assert mann_kendall([]) is None
    assert mann_kendall([1.0]) is None


def test_mann_kendall_known_example_no_ties() -> None:
    """Hand-derived: S=6 (10 of 10 pairs summed by sign), n=5, no ties.

    Var(S) = n(n-1)(2n+5)/18 = 5*4*15/18 = 16.6667 exactly.
    z = (S-1)/sqrt(Var(S)) = 5/sqrt(16.6667) = 1.224744...
    p = 2*(1-Phi(z)) = 0.220671... (independently recomputed via math.erf,
    not by calling the function under test).
    tau = S / (n(n-1)/2) = 6/10 = 0.6.
    """
    result = mann_kendall([1.0, 3.0, 2.0, 5.0, 4.0])
    assert result is not None
    assert result.n == 5
    assert result.s == 6
    assert result.var_s == pytest.approx(16.666666666666668)
    assert result.tau == pytest.approx(0.6)
    expected_z = 5.0 / math.sqrt(16.666666666666668)
    assert result.z == pytest.approx(expected_z)
    expected_p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(expected_z / math.sqrt(2.0))))
    assert result.p_value == pytest.approx(expected_p)
    # tau=0.6 "looks" like a trend, but n=5 is too short to call it
    # significant at alpha=0.05 (p~0.22) — the whole point of the test.
    assert result.trend == "no trend"


def test_mann_kendall_tie_correction() -> None:
    """[1, 1, 2, 3]: S=5 (0 from the tied pair, +1 for the other 5 pairs).

    Tie correction subtracts sum(t(t-1)(2t+5)) for each tied group (one
    group of size 2 here: 2*1*9=18) from the no-tie variance
    (n(n-1)(2n+5)=4*3*13=156), giving Var(S)=(156-18)/18=7.6667, strictly
    less than the no-tie value of 156/18=8.6667.
    """
    result = mann_kendall([1.0, 1.0, 2.0, 3.0])
    assert result is not None
    assert result.s == 5
    assert result.var_s == pytest.approx(138.0 / 18.0)


def test_mann_kendall_detects_strong_monotonic_drift() -> None:
    """A real linear trend with modest noise must be flagged, with high
    confidence (tiny p-value) and the correct sign."""
    rng = np.random.default_rng(20260719)
    n = 40
    values = [50.0 + 0.8 * i + rng.normal(scale=0.3) for i in range(n)]
    result = mann_kendall(values)
    assert result is not None
    assert result.trend == "increasing"
    assert result.z > 0
    assert result.p_value < 0.001

    decreasing = [-v for v in values]
    result_dec = mann_kendall(decreasing)
    assert result_dec is not None
    assert result_dec.trend == "decreasing"
    assert result_dec.p_value < 0.001


def test_mann_kendall_stationary_series_no_trend() -> None:
    """Symmetric oscillation around a constant mean: no monotonic drift, so
    the verdict must stay "no trend" regardless of individual sample noise."""
    values = [50.0 + (0.5 if i % 2 == 0 else -0.5) for i in range(40)]
    result = mann_kendall(values)
    assert result is not None
    assert result.trend == "no trend"
    assert result.p_value >= 0.05


# --------------------------------------------------------------------------- #
# CUSUM
# --------------------------------------------------------------------------- #


def test_cusum_none_when_empty_or_degenerate_reference() -> None:
    assert cusum([], reference_mean=0.0, reference_std=1.0) is None
    assert cusum([1.0, 2.0], reference_mean=0.0, reference_std=0.0) is None
    assert cusum([1.0, 2.0], reference_mean=0.0, reference_std=-1.0) is None


def test_cusum_known_example_hand_computed() -> None:
    """values=[2.0]*10 standardized against mean=0/std=1 with k=0.5, h=5.0:
    z_i=2.0 every tick, Sh increments by 1.5/tick -> Sh=1.5,3.0,4.5,6.0,...
    First alarm (Sh>5) at index 3 (Sh=6.0); final Sh after 10 ticks = 15.0.
    """
    result = cusum([2.0] * 10, reference_mean=0.0, reference_std=1.0, k=0.5, h=5.0)
    assert result is not None
    assert result.sh == pytest.approx(15.0)
    assert result.sl == pytest.approx(0.0)
    assert result.alarm is True
    assert result.direction == "upper"
    assert result.first_alarm_index == 3


def test_cusum_lower_direction() -> None:
    result = cusum([-2.0] * 10, reference_mean=0.0, reference_std=1.0, k=0.5, h=5.0)
    assert result is not None
    assert result.alarm is True
    assert result.direction == "lower"
    assert result.sl == pytest.approx(15.0)
    assert result.sh == pytest.approx(0.0)


def test_cusum_stationary_series_never_alarms() -> None:
    """Small noise drawn from exactly the reference distribution must never
    trip the classic 0.5/5.0 sigma CUSUM over a realistic sample size."""
    rng = np.random.default_rng(20260719)
    values = list(rng.normal(loc=10.0, scale=2.0, size=200))
    result = cusum(values, reference_mean=10.0, reference_std=2.0)
    assert result is not None
    assert result.alarm is False


def test_cusum_sustained_shift_triggers_alarm_but_transient_does_not() -> None:
    """A one-tick blip must NOT alarm (that's the point of a slack/threshold
    at all); a SUSTAINED shift of several sigma must."""
    rng = np.random.default_rng(20260720)
    baseline = list(rng.normal(loc=10.0, scale=2.0, size=30))
    transient_blip = baseline + [10.0 + 3 * 2.0] + list(rng.normal(loc=10.0, scale=2.0, size=30))
    blip_result = cusum(transient_blip, reference_mean=10.0, reference_std=2.0)
    assert blip_result is not None
    assert blip_result.alarm is False

    sustained_shift = baseline + list(rng.normal(loc=10.0 + 3 * 2.0, scale=2.0, size=30))
    shift_result = cusum(sustained_shift, reference_mean=10.0, reference_std=2.0)
    assert shift_result is not None
    assert shift_result.alarm is True
    assert shift_result.direction == "upper"
    # The alarm must fire somewhere in the shifted tail, not the baseline.
    assert shift_result.first_alarm_index is not None
    assert shift_result.first_alarm_index >= len(baseline)


# --------------------------------------------------------------------------- #
# evaluate_stat_trend / evaluate_trends
# --------------------------------------------------------------------------- #


def _summary(mean: float, std: float) -> StatSummary:
    return StatSummary(
        mean=mean,
        std=std,
        quantiles={"p10": mean - std, "p50": mean, "p90": mean + std},
        n=10,
    )


def test_evaluate_stat_trend_empty_observations_returns_none() -> None:
    assert evaluate_stat_trend("stat", [], _summary(0.0, 1.0)) is None


def test_evaluate_stat_trend_uses_reference_band_and_latest() -> None:
    observations = [("s1", 10.0), ("s2", 11.0), ("s3", 12.0)]
    trend = evaluate_stat_trend("idle_rpm_mean", observations, _summary(10.0, 1.0))
    assert trend is not None
    assert trend.latest_session == "s3"
    assert trend.latest_value == 12.0
    assert trend.reference_band == (9.0, 11.0)
    assert trend.session_ids == ("s1", "s2", "s3")


def test_evaluate_trends_ragged_series_excludes_missing_sessions() -> None:
    """A session that never carried a stat (Task 4.1's omit-not-zero
    contract) must not appear in that stat's series at all."""
    reference = ReferenceEpoch(
        frozen_at="2026-07-01T00:00:00+00:00",
        sessions=("s1", "s2"),
        stats={"idle_rpm_mean": _summary(800.0, 10.0)},
    )
    fingerprints = [
        ("s1", {"idle_rpm_mean": 800.0}),
        ("s2", {}),  # missing this stat entirely
        ("s3", {"idle_rpm_mean": 850.0}),
    ]
    trends = evaluate_trends(fingerprints, reference)
    trend = trends["idle_rpm_mean"]
    assert trend.session_ids == ("s1", "s3")
    assert trend.latest_session == "s3"


def test_evaluate_trends_only_covers_reference_epoch_stats() -> None:
    reference = ReferenceEpoch(
        frozen_at="2026-07-01T00:00:00+00:00",
        sessions=("s1",),
        stats={"idle_rpm_mean": _summary(800.0, 10.0)},
    )
    fingerprints = [("s1", {"idle_rpm_mean": 800.0, "unrelated_stat": 1.0})]
    trends = evaluate_trends(fingerprints, reference)
    assert set(trends.keys()) == {"idle_rpm_mean"}
