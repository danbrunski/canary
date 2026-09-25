"""Model drift — pure functions over a model's persisted scaler / thresholds.

The model-side half of the health layer (R04.8): live-vs-training feature
distance and reconstruction-error drift. The daemon's
:class:`~src.daemon.health.HealthMonitor` calls these; everything about the
host, the bus and the telemetry queues lives there, so a change to it no
longer pays the ``ml_training`` tier.

Drift uses the scaler the model already persisted (IF manifest's z-score
``means``/``stds``): live-vs-training mean distance in training-σ units is a
cheap, dependency-free population-stability proxy. No scipy / numpy-heavy
PSI machinery — the Pi budget (NFR-01) rules that out and the σ-distance is
enough to fire a "retrain me" warning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["FeatureDriftReport", "feature_drift", "reconstruction_error_drift"]


@dataclass(frozen=True)
class FeatureDriftReport:
    """Per-feature live-vs-training mean distance in training-σ units."""

    per_feature_sigma: dict[str, float]
    max_sigma: float
    drifted_features: list[str]
    missing_features: list[str]  # trained columns absent from the live stats
    schema_mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_feature_sigma": self.per_feature_sigma,
            "max_sigma": self.max_sigma,
            "drifted_features": self.drifted_features,
            "missing_features": self.missing_features,
            "schema_mismatch": self.schema_mismatch,
        }


def feature_drift(
    live_means: Mapping[str, float],
    scaler: Mapping[str, Any],
    *,
    sigma_threshold: float = 3.0,
) -> FeatureDriftReport:
    """How far the live feature means have moved from training, in σ units.

    ``scaler`` is the IF/LSTM manifest's z-score block: ``means`` /
    ``stds`` / ``columns`` (the names the trainers persist). A feature is
    "drifted" when ``|live_mean − train_mean| / train_std`` exceeds
    ``sigma_threshold``. A trained column with no live mean yet is reported
    as ``missing`` (warmup or a vanished signal) and forces
    ``schema_mismatch`` so the dashboard can't show a green light over a
    half-blind model.
    """
    means = scaler.get("means") or scaler.get("mean") or {}
    stds = scaler.get("stds") or scaler.get("std") or {}
    columns = list(scaler.get("columns") or scaler.get("feature_columns") or means.keys())
    per_feature: dict[str, float] = {}
    missing: list[str] = []
    for col in columns:
        if col not in live_means:
            missing.append(col)
            continue
        train_mean = float(means.get(col, 0.0))
        train_std = float(stds.get(col, 0.0))
        if train_std <= 0.0:
            # A constant training feature: any movement is "infinite" σ but
            # report it as the absolute delta so the number stays finite.
            per_feature[col] = round(abs(live_means[col] - train_mean), 6)
        else:
            per_feature[col] = round(abs(live_means[col] - train_mean) / train_std, 4)
    drifted = sorted(c for c, v in per_feature.items() if v > sigma_threshold)
    return FeatureDriftReport(
        per_feature_sigma=per_feature,
        max_sigma=max(per_feature.values(), default=0.0),
        drifted_features=drifted,
        missing_features=sorted(missing),
        schema_mismatch=bool(missing),
    )


def reconstruction_error_drift(
    live_mean_error: float | None,
    thresholds: Mapping[str, Any],
) -> dict[str, float | None]:
    """Live mean reconstruction error vs the LSTM's calibrated training mean.

    ``thresholds`` is the LSTM manifest ``thresholds.global`` block (carries
    the training ``mean``/``std``/``threshold``). Returns the ratio of live
    error to training mean and how many training-σ above it the live error
    sits — a steadily rising ratio is the "the world changed, retrain"
    signal even while individual windows stay under threshold.
    """
    if live_mean_error is None:
        return {"live_mean_error": None, "ratio": None, "sigma_above_train": None}
    glob = thresholds.get("global", thresholds) if isinstance(thresholds, Mapping) else {}
    train_mean = float(glob.get("mean", 0.0)) if isinstance(glob, Mapping) else 0.0
    train_std = float(glob.get("std", 0.0)) if isinstance(glob, Mapping) else 0.0
    ratio = (live_mean_error / train_mean) if train_mean > 0 else None
    sigma = ((live_mean_error - train_mean) / train_std) if train_std > 0 else None
    return {
        "live_mean_error": round(live_mean_error, 6),
        "ratio": None if ratio is None else round(ratio, 4),
        "sigma_above_train": None if sigma is None else round(sigma, 4),
    }
