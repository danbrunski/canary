"""Synthetic-fault detector-quality eval (Phase 7.5).

Builds the "did the detector actually fire on a known fault, and how often
does it cry wolf on clean data?" answer the plan asks for. For each fault
kind in :data:`DEFAULT_FAULT_KINDS` (the four real sensor-failure modes
plus the multiplicative *scale* fault), this:

* injects the fault into a copy of a clean *resampled* wide frame,
* re-runs the shared feature builder (so the rolling features pick up the
  injected values exactly like training does),
* scores with the trained Isolation Forest and the fused stream (the IF
  half of fusion is exercised even when no LSTM/behavior sources are
  available, so the regression guard sees the fusion code path),
* records detection (any flag inside the injection window) and counts
  alerts outside any window for the nominal false-positive rate.

The harness is intentionally IF-focused so it stays fast and torch-free in
CI; the LSTM training-completion + per-mode threshold-population is graded
by a separate dedicated test. The report is JSON-safe so the training
ledger / dashboard can consume it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.alerts.fusion import FusionScorer, isolation_forest_source
from src.config import CanaryConfig
from src.models.isolation_forest import (
    CALIBRATED_ANOMALY_COLUMN,
    CALIBRATED_THRESHOLD_COLUMN,
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
)
from src.preprocessing.anomaly_injection import AnomalyInjector, AnomalyKind
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.resampler import TIMESTAMP_COLUMN

DEFAULT_FAULT_KINDS: tuple[AnomalyKind, ...] = (
    "spike",
    "drift",
    "dropout",
    "stuck",
    "scale",
)


@dataclass(frozen=True)
class _DetectorMetrics:
    detector: str
    fault_kind: str
    runs: int
    detected: int

    @property
    def detection_rate(self) -> float:
        return self.detected / self.runs if self.runs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "fault_kind": self.fault_kind,
            "runs": self.runs,
            "detected": self.detected,
            "detection_rate": self.detection_rate,
        }


def _if_anomalous(scored: pd.DataFrame) -> pd.Series:
    """Boolean per-row IF verdict mirroring ``emitter.emit_isolation_forest_row``:
    calibrated when present, legacy ``-1`` label fallback otherwise, with
    missing-features rows always anomalous (they aren't scoreable)."""
    missing = scored[MISSING_FEATURES_COLUMN].astype(bool)
    calib = scored[CALIBRATED_ANOMALY_COLUMN]
    if calib.notna().any():
        return missing | calib.fillna(False).astype(bool)
    return missing | (scored[LABEL_COLUMN].fillna(1).astype(int) == -1)


def _fused_anomalous(
    scored: pd.DataFrame,
    *,
    fusion: FusionScorer,
    scales: dict[str, float],
) -> pd.Series:
    """Boolean fused-anomaly per row using IF as the only fusion source.

    With one source the fused severity reduces to the IF source's own
    contribution + the configured emit threshold; surfacing it through the
    actual ``FusionScorer`` path is what the regression guard wants — the
    behavior is identical to ``isolation_forest_source(...).fired`` at the
    default config, but routed through the production fusion code."""
    out = np.zeros(len(scored), dtype=bool)
    for i in range(len(scored)):
        row = scored.iloc[i]
        if bool(row.get(MISSING_FEATURES_COLUMN, False)):
            out[i] = True
            continue
        mode = row.get(MODEL_MODE_COLUMN)
        threshold = row.get(CALIBRATED_THRESHOLD_COLUMN)
        if mode is None or pd.isna(mode) or pd.isna(threshold):
            continue
        score = row.get(SCORE_COLUMN)
        if pd.isna(score):
            continue
        ifsrc = isolation_forest_source(
            float(score),
            float(threshold),
            scale=scales.get(str(mode)),
        )
        out[i] = fusion.fuse([ifsrc], timestamp="", operating_mode=str(mode)).is_anomaly
    return pd.Series(out, index=scored.index)


def _hours_covered(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    span = (timestamps.max() - timestamps.min()).total_seconds()
    return max(span / 3600.0, 1.0 / 3600.0)  # floor at 1 second to avoid div-by-zero


def _inject_one(
    resampled: pd.DataFrame,
    *,
    kind: AnomalyKind,
    channel: str,
    rng: np.random.Generator,
) -> AnomalyInjector:
    """Inject one fault of ``kind`` into a copy of ``resampled``.

    Window placement is deterministic-with-jitter from ``rng`` so multiple
    runs cover different stretches of the frame. Fault magnitudes are sized
    to be unambiguously anomalous given the channel's nominal spread (this
    is *detector quality* eval, not threshold-edge calibration — 7.5's harness
    grades "do the detectors see a clear fault at all", not "how close to
    the boundary can we get")."""
    frame = resampled.copy()
    inj = AnomalyInjector(frame)
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    n = len(timestamps)
    window_len = max(5, n // 10)
    # Place the window in the middle ± a jittered offset, bounded inside.
    centre = n // 2 + int(rng.integers(-n // 8, n // 8 + 1))
    centre = max(window_len, min(n - window_len - 1, centre))
    start = timestamps.iloc[centre - window_len // 2]
    end = timestamps.iloc[centre + window_len // 2]
    series = frame[channel].dropna()
    nominal_max = float(series.max()) if not series.empty else 1.0
    nominal_std = float(series.std()) if not series.empty else 1.0
    if kind == "spike":
        inj.spike(channel=channel, start=start, end=end, value=nominal_max * 5 + 1_000)
    elif kind == "drift":
        inj.drift(channel=channel, start=start, end=end, total_delta=nominal_std * 10 + 100)
    elif kind == "dropout":
        inj.dropout(channel=channel, start=start, end=end)
    elif kind == "stuck":
        inj.stuck(channel=channel, start=start, end=end)
    elif kind == "scale":
        inj.scale(channel=channel, start=start, end=end, factor=5.0)
    else:
        raise ValueError(f"unsupported fault kind: {kind!r}")
    return inj


def evaluate_detectors(
    resampled: pd.DataFrame,
    config: CanaryConfig,
    trained_if: TrainedIsolationForest,
    *,
    fault_channel: str,
    fault_kinds: Sequence[AnomalyKind] = DEFAULT_FAULT_KINDS,
    n_injections: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the synthetic-fault harness and return a JSON-safe report.

    ``resampled`` is the clean wide frame *before* ``featurize_wide_frame``
    (the harness re-featurizes after each injection so rolling stats reflect
    the fault). ``trained_if`` is the model the harness grades. Returns
    a dict with ``per_fault`` metrics for each detector + a ``nominal_fp``
    block measured on the unmodified frame.
    """
    rng = np.random.default_rng(seed)
    fusion = FusionScorer(config.models.fusion)
    scales = dict(trained_if.score_scales)

    # Nominal: one featurize + score pass — this is the FP rate every other
    # injection run is measured against.
    nominal_features = featurize_wide_frame(resampled, config).features
    nominal_scored = trained_if.score(nominal_features)
    nominal_if = _if_anomalous(nominal_scored)
    nominal_fused = _fused_anomalous(nominal_scored, fusion=fusion, scales=scales)
    hours = _hours_covered(nominal_features)
    nominal_fp = {
        "isolation_forest": {
            "alerts": int(nominal_if.sum()),
            "rows": int(len(nominal_if)),
            "hours": hours,
            "per_hour": round(float(nominal_if.sum()) / hours, 4),
        },
        "fused": {
            "alerts": int(nominal_fused.sum()),
            "rows": int(len(nominal_fused)),
            "hours": hours,
            "per_hour": round(float(nominal_fused.sum()) / hours, 4),
        },
    }

    per_fault: list[dict[str, Any]] = []
    for kind in fault_kinds:
        if_hits = 0
        fused_hits = 0
        for _ in range(n_injections):
            inj = _inject_one(resampled, kind=kind, channel=fault_channel, rng=rng)
            featurized = featurize_wide_frame(inj.df, config).features
            scored = trained_if.score(featurized)
            timestamps = pd.to_datetime(featurized[TIMESTAMP_COLUMN], utc=True)
            window = inj.windows[0]
            in_window = (timestamps >= window.start) & (timestamps < window.end)
            if not in_window.any():
                continue
            if_hits += int(_if_anomalous(scored).loc[in_window].any())
            fused_hits += int(
                _fused_anomalous(scored, fusion=fusion, scales=scales).loc[in_window].any()
            )
        per_fault.append(
            _DetectorMetrics(
                detector="isolation_forest",
                fault_kind=kind,
                runs=n_injections,
                detected=if_hits,
            ).to_dict()
        )
        per_fault.append(
            _DetectorMetrics(
                detector="fused",
                fault_kind=kind,
                runs=n_injections,
                detected=fused_hits,
            ).to_dict()
        )

    return {
        "fault_kinds": list(fault_kinds),
        "n_injections": n_injections,
        "seed": seed,
        "nominal_fp": nominal_fp,
        "per_fault": per_fault,
    }


__all__ = ["DEFAULT_FAULT_KINDS", "evaluate_detectors"]
