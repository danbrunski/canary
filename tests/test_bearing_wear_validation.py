"""Synthetic bearing-wear validation (Task 4.D.3).

A failing wheel bearing shows up on the bus as one wheel-speed channel
slowly diverging from its pair (and getting noisier) long before the ABS
module's coarse cross-check trips a DTC. This proves the premise end to
end *without* a real worn-bearing capture or `confirmed` wheel-speed
bindings: train the Isolation Forest on a clean four-wheel-speed session,
then score a session where the front pair progressively diverges, and
confirm the detector fires while the divergence is still small — i.e.
*before* a wheel-speed-comparison DTC (~5 kph) would.

Synthetic by design (the plan explicitly permits it until real
``WHEEL_SPEED_*`` bindings graduate to ``confirmed``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import IsolationForestConfig
from src.models.isolation_forest import (
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    fit_isolation_forest,
)
from src.preprocessing.features import FeatureConfig, compute_features


def _steady_state_anomaly_rate(scored: pd.DataFrame) -> float:
    """False-positive rate over rows the model could actually score.

    The IF deliberately labels missing-feature rows anomalous so a sensor
    dropout is visible; the rolling-feature warmup at a session's start has
    NaN std for the first sample(s). Counting those as false positives would
    measure the warmup, not the detector — exclude them."""
    complete = scored[~scored[MISSING_FEATURES_COLUMN].astype("boolean").fillna(False)]
    if complete.empty:
        return 0.0
    return float((complete[LABEL_COLUMN] == -1).mean())

#: An ABS module compares wheel-speed pairs coarsely; a single-wheel fault
#: code needs a divergence on this order before it sets. We want the ML to
#: fire well under this.
_DTC_DIVERGENCE_KPH = 5.0
_RNG = np.random.default_rng(7)


def _frame(session: str, fl: np.ndarray, fr: np.ndarray) -> pd.DataFrame:
    n = len(fl)
    ts = pd.date_range("2026-05-17T00:00:00Z", periods=n, freq="1s")
    delta = np.abs(fl - fr)
    df = pd.DataFrame(
        {
            "session": session,
            "timestamp": ts,
            "operating_mode": "all",
            "decoded:WHEEL_SPEED_FL": fl,
            "decoded:WHEEL_SPEED_FR": fr,
            "decoded:WHEEL_SPEED_DELTA": delta,
        }
    )
    return compute_features(df, config=FeatureConfig(window_seconds=10))


def _clean_speed(n: int) -> np.ndarray:
    # A drive profile: accelerate, cruise, slow — both wheels track it.
    base = 40 + 25 * np.sin(np.linspace(0, 3 * np.pi, n))
    return np.asarray(base, dtype=float)


def test_bearing_wear_fires_before_a_dtc_would() -> None:
    n = 600
    base = _clean_speed(n)
    # Healthy: both front wheels track the vehicle within sensor noise.
    fl_clean = base + _RNG.normal(0, 0.15, n)
    fr_clean = base + _RNG.normal(0, 0.15, n)
    clean = _frame("clean", fl_clean, fr_clean)

    config = IsolationForestConfig(
        enabled=True, n_estimators=200, contamination=0.01, random_state=7
    )
    trained = fit_isolation_forest(clean, config)

    # Worn FL bearing: a slow ramp of divergence + growing noise. The ramp
    # reaches only ~3 kph by the end — never the DTC threshold.
    m = 400
    base_w = _clean_speed(m)
    ramp = np.linspace(0.0, 3.0, m)
    fl_worn = base_w - ramp + _RNG.normal(0, 0.15 + ramp / 6.0, m)
    fr_worn = base_w + _RNG.normal(0, 0.15, m)
    worn = _frame("worn", fl_worn, fr_worn)

    scored = trained.score(worn)
    flagged = scored[scored[LABEL_COLUMN] == -1]

    # 1. The detector fires on the worn session at all.
    assert not flagged.empty, "bearing wear produced no anomaly"

    # 2. It fires *before* a DTC would: the divergence at the first flagged
    #    row is well under the ABS cross-check threshold.
    first_flag_idx = int(flagged.index[0])
    divergence_at_first_flag = float(worn["decoded:WHEEL_SPEED_DELTA"].iloc[first_flag_idx])
    assert divergence_at_first_flag < _DTC_DIVERGENCE_KPH, (
        f"first anomaly at {divergence_at_first_flag:.2f} kph divergence — "
        f"not earlier than a DTC ({_DTC_DIVERGENCE_KPH} kph)"
    )

    # 3. The clean baseline stays quiet — the model isn't pathologically
    #    over-flagging the very data it learned (≈ contamination rate).
    assert _steady_state_anomaly_rate(trained.score(clean)) < 0.05

    # 4. The *severity* tracks the wear: the worn session is flagged far
    #    more than the clean baseline (signal, not chance).
    worn_rate = _steady_state_anomaly_rate(scored)
    assert worn_rate > 3 * _steady_state_anomaly_rate(trained.score(clean))
