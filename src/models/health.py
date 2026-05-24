"""Model drift + inference health monitoring (Task 6.27).

A detector that's silently degrading (feature distribution drifted off the
training set, reconstruction error creeping up, the Pi pegged, the schema
quietly mismatched, half the windows missing features) is worse than no
detector — it looks healthy on the dashboard while missing real events.
This module is the observability layer that makes that visible.

Two parts:

* **Pure functions** — :func:`feature_drift`, :func:`reconstruction_error_drift`,
  :func:`system_metrics`. No state, no daemon coupling; testable in isolation.
* **`HealthMonitor`** — a small rolling accumulator the daemon feeds from the
  paths it already runs (inference tick → latency / missing-feature flag /
  data freshness; model-manager poll → swap outcomes). :meth:`snapshot`
  folds in the system + drift metrics and is what ``/status`` surfaces.

Drift uses the scaler the model already persisted (IF manifest's z-score
``means``/``stds``): live-vs-training mean distance in training-σ units is a
cheap, dependency-free population-stability proxy. No scipy / numpy-heavy
PSI machinery — the Pi budget (NFR-01) rules that out and the σ-distance is
enough to fire a "retrain me" warning.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "HealthMonitor",
    "feature_drift",
    "reconstruction_error_drift",
    "system_metrics",
]


# ---------------------------------------------------------------------------
# System metrics — best-effort, no psutil dependency (Pi target)
# ---------------------------------------------------------------------------


def system_metrics() -> dict[str, float | None]:
    """Coarse host load/memory/temperature. Linux ``/proc`` + ``getloadavg``
    + thermal ``/sys``; ``None`` for any field the platform doesn't expose
    (never raises). Note: ``cpu_percent`` (a delta over time) lives on
    :class:`HealthMonitor`, not here — these reads are all single-shot."""
    out: dict[str, float | None] = {
        "load_1m": None,
        "cpu_count": None,
        "load_per_cpu": None,
        "mem_used_fraction": None,
        "mem_total_mb": None,
        "cpu_temp_c": _cpu_temp_c(),
    }
    try:
        load1 = os.getloadavg()[0]
        out["load_1m"] = round(load1, 3)
        cpus = os.cpu_count()
        if cpus:
            out["cpu_count"] = float(cpus)
            out["load_per_cpu"] = round(load1 / cpus, 3)
    except (OSError, AttributeError):
        pass
    try:
        meminfo: dict[str, float] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = float(rest.strip().split()[0])  # kB
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        if total:
            out["mem_total_mb"] = round(total / 1024, 1)
            if available is not None:
                out["mem_used_fraction"] = round((total - available) / total, 4)
    except (OSError, ValueError, IndexError):
        pass
    return out


def _cpu_temp_c() -> float | None:
    """SoC temperature in °C from the Linux thermal sysfs — the Raspberry Pi
    exposes its CPU zone at ``thermal_zone0``. ``None`` when no thermal zone
    is present (e.g. a dev laptop / container)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            milli = float(fh.read().strip())
    except (OSError, ValueError):
        return None
    return round(milli / 1000.0, 1)


def _read_cpu_times() -> tuple[float, float] | None:
    """``(idle_jiffies, total_jiffies)`` from ``/proc/stat``'s aggregate
    ``cpu`` line, or ``None`` when unavailable. A single reading is
    meaningless — the caller diffs two to get utilization over the interval."""
    try:
        with open("/proc/stat") as fh:
            fields = fh.readline().split()
    except OSError:
        return None
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    try:
        vals = [float(x) for x in fields[1:]]
    except ValueError:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
    return idle, sum(vals)


# ---------------------------------------------------------------------------
# Drift — pure functions over the persisted scaler / thresholds
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Rolling monitor — fed from the daemon's existing paths
# ---------------------------------------------------------------------------


@dataclass
class _Rolling:
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    scores: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    observed: int = 0
    missing_feature_rows: int = 0


class HealthMonitor:
    """Rolling inference-health accumulator.

    Single-writer (the daemon's inference task) like the emitter — not
    independently locked. Cheap: bounded deques, integer counters. The
    daemon calls :meth:`observe_inference` per tick, :meth:`note_swap` from
    the model-manager poll, and exposes :meth:`snapshot` through ``/status``.
    """

    def __init__(self) -> None:
        self._roll = _Rolling()
        self._last_data_ts: float | None = None
        self._swaps: dict[str, int] = {"swapped": 0, "rejected": 0, "unchanged": 0, "error": 0}
        self._last_swap_version: str | None = None
        self._obd_scheduler: dict[str, Any] | None = None
        self._cpu_prev: tuple[float, float] | None = None

    def _cpu_percent(self) -> float | None:
        """Busy fraction (0..1) of total CPU time since the previous
        snapshot. ``None`` on the first call (no baseline yet) or where
        ``/proc/stat`` is absent. Stateful by necessity — utilization is a
        delta between two readings, not an instantaneous level."""
        sample = _read_cpu_times()
        if sample is None:
            return None
        prev, self._cpu_prev = self._cpu_prev, sample
        if prev is None:
            return None
        d_idle = sample[0] - prev[0]
        d_total = sample[1] - prev[1]
        if d_total <= 0:
            return None
        return round(max(0.0, min(1.0, 1.0 - d_idle / d_total)), 4)

    def observe_inference(
        self,
        *,
        latency_ms: float | None = None,
        missing_features: bool = False,
        score: float | None = None,
        data_timestamp_epoch: float | None = None,
    ) -> None:
        self._roll.observed += 1
        if latency_ms is not None:
            self._roll.latencies_ms.append(float(latency_ms))
        if score is not None:
            self._roll.scores.append(float(score))
        if missing_features:
            self._roll.missing_feature_rows += 1
        if data_timestamp_epoch is not None:
            self._last_data_ts = data_timestamp_epoch

    def note_swap(self, kind: str, *, version: str | None = None) -> None:
        self._swaps[kind] = self._swaps.get(kind, 0) + 1
        if kind == "swapped" and version is not None:
            self._last_swap_version = version

    def observe_obd_scheduler(self, metrics: Any) -> None:
        """Record the latest adaptive OBD scheduler snapshot for ``/status``."""
        if hasattr(metrics, "to_dict"):
            self._obd_scheduler = dict(metrics.to_dict())
        else:
            self._obd_scheduler = dict(metrics)

    def snapshot(
        self,
        *,
        now_epoch: float | None = None,
        scaler: Mapping[str, Any] | None = None,
        live_means: Mapping[str, float] | None = None,
        lstm_thresholds: Mapping[str, Any] | None = None,
        live_mean_recon_error: float | None = None,
    ) -> dict[str, Any]:
        """JSON-safe health block for ``/status``.

        Optional drift inputs are folded in only when provided — the daemon
        passes the running model's scaler + recent live means when it has
        them, and the block degrades gracefully (``None``/absent) otherwise.
        """
        lat = sorted(self._roll.latencies_ms)
        observed = self._roll.observed
        snap: dict[str, Any] = {
            "observed": observed,
            "inference_latency_ms": {
                "mean": round(sum(lat) / len(lat), 3) if lat else None,
                "p95": _percentile(lat, 0.95),
                "max": max(lat) if lat else None,
            },
            "missing_feature_rate": (
                round(self._roll.missing_feature_rows / observed, 4) if observed else None
            ),
            "data_freshness_seconds": (
                None
                if self._last_data_ts is None
                else round(max(0.0, (now_epoch or _now()) - self._last_data_ts), 2)
            ),
            "model_swaps": dict(self._swaps),
            "active_model_version": self._last_swap_version,
            "system": system_metrics(),
        }
        snap["system"]["cpu_percent"] = self._cpu_percent()
        if self._obd_scheduler is not None:
            snap["obd_scheduler"] = dict(self._obd_scheduler)
        if scaler is not None and live_means is not None:
            snap["feature_drift"] = feature_drift(live_means, scaler).to_dict()
        if lstm_thresholds is not None:
            snap["reconstruction_error_drift"] = reconstruction_error_drift(
                live_mean_recon_error, lstm_thresholds
            )
        return snap


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1))))
    return round(sorted_values[idx], 3)


def _now() -> float:
    from datetime import UTC, datetime

    return datetime.now(UTC).timestamp()
