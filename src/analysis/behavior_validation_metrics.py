"""Behavior-validation metrics in physical units (R20; B02.5/B04.1).

Downstream of row evaluation: denominators, MAE/bias/quantiles, events after
the persistence gate, wall-clock exposure, the training-fitted simple baseline,
the trusted-envelope coverage, per-drive spread and the drive-resampled
bootstrap (whole drives, never adjacent seconds), and the reporting strata.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.behavior_validation_evaluation import (
    EventPolicy,
    RowEvaluation,
    model_event_times,
)
from src.analysis.behavior_validation_schema import RegimeAxis, ReportRegimes
from src.analysis.splits import DEFAULT_JOIN_GAP_SECONDS
from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    FoldMetrics,
    RobustBaselineFit,
)
from src.behavior.surface_eval import bin_indices, containing_cell_indices, trusted_cell_mask
from src.columns import SESSION_COLUMN, gap_column
from src.dataframe_utils import numeric_values
from src.preprocessing.features import compute_rate_of_change

#: Drives needed before a drive-resampled bootstrap interval is reported.
MIN_BOOTSTRAP_DRIVES = 3


#: Evaluated rows a cell / decile needs to count as visited (envelope coverage).
ENVELOPE_MIN_ROWS = 5


# ---------------------------------------------------------------------------
# Baselines, envelope
# ---------------------------------------------------------------------------


BaselineKind = Literal["training_median", "training_mode_median"]


def fit_baseline(actual: np.ndarray, modes: np.ndarray, kind: BaselineKind) -> dict[str, float]:
    """Training-fitted simple baseline: robust median, optionally per mode."""
    finite = np.isfinite(actual)
    if not finite.any():
        return {}
    baseline = {"__all__": float(np.median(actual[finite]))}
    if kind == "training_mode_median":
        for mode in pd.unique(modes[finite]):
            values = actual[finite & (modes == mode)]
            if values.size:
                baseline[str(mode)] = float(np.median(values))
    return baseline


def baseline_predictions(baseline: Mapping[str, float], modes: np.ndarray) -> np.ndarray:
    if not baseline:
        return np.full(len(modes), np.nan)
    fallback = baseline["__all__"]
    return np.asarray([baseline.get(str(mode), fallback) for mode in modes], dtype=float)


def envelope_coverage(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    min_rows: int = ENVELOPE_MIN_ROWS,
) -> float | None:
    """Fraction of the model's trusted operating envelope the rows visited.

    2D: trusted cells (the cells serving uses) with ``min_rows`` evaluated
    rows. 1D lookup/poly: domain deciles. Robust constant baselines have no
    envelope (``None``).
    """
    evaluated = ev.evaluated
    if isinstance(model, BehaviorModel2D):
        trusted = trusted_cell_mask(model)
        if not trusted.any():
            return 0.0
        idx = containing_cell_indices(model, ev.x[evaluated], ev.y[evaluated])
        counts = np.bincount(idx[idx >= 0], minlength=len(trusted))
        return float(((counts >= min_rows) & trusted).sum() / trusted.sum())
    if isinstance(model.fit, RobustBaselineFit):
        return None
    lo, hi = model.axis.domain
    edges = np.linspace(lo, hi, 11)
    bins = bin_indices(ev.x[evaluated], edges)
    counts = np.bincount(bins, minlength=10)
    return float((counts >= min_rows).sum() / 10)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _r_squared(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    if actual.size < 2:
        return None
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    if denominator <= 0:
        return None
    return float(1.0 - np.sum((actual - predicted) ** 2) / denominator)


def _median_or_none(values: np.ndarray) -> float | None:
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if finite.size else None


def _evaluated_hours(
    seconds: np.ndarray,
    evaluated: np.ndarray,
    drives: np.ndarray,
    *,
    interval_seconds: float,
    break_seconds: float = DEFAULT_JOIN_GAP_SECONDS,
) -> float:
    """Wall-clock exposure of the evaluated rows, in hours (B02.5).

    An event *rate* needs the time the detector was actually watching, not a
    row count: a slowly polled output (the catalyst PID on its slow tier) is
    evaluated in 1 s pairs ~17 s apart, and ``rows / 3600`` would call that
    exposure a twentieth of what it was. Per drive, first-to-last evaluated
    time plus one tick (so a lone row counts one interval and contiguous
    1 Hz rows give exactly the old ``n`` seconds), summed over drives.

    A gap between consecutive evaluated rows longer than ``break_seconds``
    (the drive join gap) ends one watch and starts the next: a directory
    that kept recording a parked truck overnight is one "drive" to
    :func:`group_drives`, and its span is not time anything was watched
    (EVALUATOR_VERSION 3).
    """
    total = 0.0
    for drive in set(drives[evaluated].tolist()):
        stamps = seconds[evaluated & (drives == drive)]
        stamps = np.sort(stamps[np.isfinite(stamps)])
        if not stamps.size:
            continue
        breaks = np.flatnonzero(np.diff(stamps) > break_seconds)
        starts = np.concatenate(([0], breaks + 1))
        ends = np.concatenate((breaks, [stamps.size - 1]))
        total += float(np.sum(stamps[ends] - stamps[starts])) + interval_seconds * starts.size
    return total / 3600.0


def fold_metrics(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
    bootstrap_seed: int = 0,
) -> FoldMetrics:
    """Denominators, physical-unit accuracy and events over ``ev``'s rows."""
    evaluated = ev.evaluated
    eligible = ev.eligible
    drives = np.asarray([drive_of_piece.get(str(p), str(p)) for p in ev.pieces], dtype=object)
    refusals = Counter(str(r) for r in ev.reasons[~evaluated])
    residual = ev.residual[evaluated]
    abs_err = np.abs(residual)
    actual = ev.actual[evaluated]
    expected = ev.expected[evaluated]
    n = int(evaluated.sum())
    severity = ev.severity[evaluated]
    metrics: dict[str, Any] = {
        "drives": len(set(drives[evaluated].tolist())),
        "rows": len(ev.reasons),
        "input_present": int(ev.present.sum()),
        "fresh": int(ev.fresh.sum()),
        "eligible": int(eligible.sum()),
        "evaluated": n,
        "coverage": (n / int(eligible.sum())) if eligible.any() else None,
        "refusals": dict(sorted(refusals.items(), key=lambda kv: (-kv[1], kv[0]))),
        "evaluated_hours": _evaluated_hours(
            ev.seconds,
            evaluated,
            drives,
            interval_seconds=policy.interval_seconds,
            break_seconds=policy.exposure_break_seconds,
        ),
        "envelope_coverage": envelope_coverage(model, ev),
        "axis_pairing_s": None if ev.axis_pairing_s is None else dict(ev.axis_pairing_s),
    }
    if n:
        metrics.update(
            mae=float(abs_err.mean()),
            bias=float(residual.mean()),
            abs_error_p50=float(np.quantile(abs_err, 0.50)),
            abs_error_p90=float(np.quantile(abs_err, 0.90)),
            abs_error_p95=float(np.quantile(abs_err, 0.95)),
            r_squared=_r_squared(actual, expected),
            warning_half_width_median=_median_or_none(ev.warning_half_width[evaluated]),
            critical_half_width_median=_median_or_none(ev.critical_half_width[evaluated]),
            warning_row_rate=float(np.mean(severity == "warning")),
            critical_row_rate=float(np.mean(severity == "critical")),
        )
        events = len(model_event_times(model, ev, drives, policy))
        metrics["events"] = events
        hours = metrics["evaluated_hours"]
        metrics["events_per_hour"] = events / hours if hours > 0 else None
        if baseline:
            base = baseline_predictions(baseline, ev.modes[evaluated])
            same = np.isfinite(base)
            if same.any():
                base_mae = float(np.abs(actual[same] - base[same]).mean())
                model_mae = float(abs_err[same].mean())
                metrics["baseline_mae"] = base_mae
                metrics["skill_vs_baseline"] = 1.0 - model_mae / base_mae if base_mae > 0 else None
        per_drive = {
            drive: float(abs_err[drives[evaluated] == drive].mean())
            for drive in pd.unique(drives[evaluated])
        }
        metrics["drive_mae_min"] = min(per_drive.values())
        metrics["drive_mae_max"] = max(per_drive.values())
        if len(per_drive) >= MIN_BOOTSTRAP_DRIVES:
            metrics["mae_drive_bootstrap_90"] = _drive_bootstrap(
                abs_err, drives[evaluated], seed=bootstrap_seed
            )
    return FoldMetrics(**metrics)


def _drive_bootstrap(
    abs_err: np.ndarray, drives: np.ndarray, *, seed: int, reps: int = 2000
) -> list[float]:
    """90 % interval of pooled MAE, resampling whole drives (not rows)."""
    rng = np.random.default_rng(seed)
    labels = pd.unique(drives)
    sums = np.asarray([abs_err[drives == d].sum() for d in labels])
    counts = np.asarray([(drives == d).sum() for d in labels], dtype=float)
    draws = rng.integers(0, len(labels), size=(reps, len(labels)))
    pooled = sums[draws].sum(axis=1) / counts[draws].sum(axis=1)
    return [float(np.quantile(pooled, 0.05)), float(np.quantile(pooled, 0.95))]


def qualifying_drive_count(
    ev: RowEvaluation, drive_of_piece: Mapping[str, str], minimum: int
) -> int:
    evaluated = ev.evaluated
    drives = [drive_of_piece.get(str(p), str(p)) for p in ev.pieces[evaluated]]
    return sum(1 for count in Counter(drives).values() if count >= minimum)


def breakdown(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    *,
    by: Literal["drive", "regime"],
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, FoldMetrics]:
    keys = (
        np.asarray([drive_of_piece.get(str(p), str(p)) for p in ev.pieces], dtype=object)
        if by == "drive"
        else ev.modes
    )
    return breakdown_by_labels(
        model, ev, keys, drive_of_piece=drive_of_piece, policy=policy, baseline=baseline
    )


def breakdown_by_labels(
    model: BehaviorModel1D | BehaviorModel2D,
    ev: RowEvaluation,
    labels: np.ndarray,
    *,
    drive_of_piece: Mapping[str, str],
    policy: EventPolicy,
    baseline: Mapping[str, float] | None = None,
) -> dict[str, FoldMetrics]:
    """:func:`fold_metrics` per label (one label per row of ``ev``).

    Only groups the model could have spoken for: a group with no eligible
    row says nothing about accuracy (its refusals are in the fold totals).
    Events restart per drive *within* a group, so a stratum's event count is
    the persistence gate run on that stratum's rows only.
    """
    eligible = ev.eligible
    return {
        str(key): fold_metrics(
            model,
            ev.subset(labels == key),
            drive_of_piece=drive_of_piece,
            policy=policy,
            baseline=baseline,
        )
        for key in pd.unique(labels[eligible])
    }


def _bin_labels(values: np.ndarray, axis: RegimeAxis) -> np.ndarray:
    labels = np.full(len(values), "missing", dtype=object)
    edges = axis.edges
    finite = np.isfinite(values)
    labels[finite & (values < edges[0])] = f"<{edges[0]:g} {axis.unit}"
    labels[finite & (values >= edges[-1])] = f">={edges[-1]:g} {axis.unit}"
    for lo, hi in zip(edges, edges[1:], strict=False):
        labels[finite & (values >= lo) & (values < hi)] = f"[{lo:g},{hi:g}) {axis.unit}"
    return labels


def regime_labels(
    frame: pd.DataFrame,
    regimes: ReportRegimes | None,
    *,
    revision_of_piece: Callable[[str], str | None] | None = None,
) -> dict[str, np.ndarray]:
    """Per-row stratum labels for evaluation reports (B04.1).

    ``calibration`` is the tune revision of each row's session (``unknown``
    when the registry cannot place it). Axis strata label a stale or absent
    channel ``stale`` / ``missing`` — an unavailable context is its own
    stratum, never folded into a numeric bin. The steady-state split reads
    each rate from :func:`src.preprocessing.features.compute_rate_of_change`
    and labels a row ``transient`` if any listed rate exceeds its limit,
    ``rate_unavailable`` if any rate is undefined, else ``steady``.
    """
    from src.columns import roc_column

    out: dict[str, np.ndarray] = {}
    if revision_of_piece is not None and SESSION_COLUMN in frame.columns:
        pieces = frame[SESSION_COLUMN].astype(str).to_numpy()
        cache: dict[str, str] = {}
        revisions: list[str] = []
        for piece in pieces:
            if piece not in cache:
                cache[piece] = revision_of_piece(piece) or "unknown"
            revisions.append(cache[piece])
        out["calibration"] = np.asarray(revisions, dtype=object)
    if regimes is None:
        return out
    for name, axis in regimes.axes.items():
        labels = _bin_labels(numeric_values(frame, axis.channel), axis)
        gap = gap_column(axis.channel)
        if gap in frame.columns:
            stale = frame[gap].fillna(True).astype(bool).to_numpy()
            labels[stale] = "stale"
        out[name] = labels
    steady = regimes.steady_state
    if steady is not None:
        channels = list(steady.max_abs_rate_per_second)
        missing_roc = [c for c in channels if roc_column(c) not in frame.columns]
        rates = compute_rate_of_change(frame, missing_roc) if missing_roc else frame
        transient = np.zeros(len(frame), dtype=bool)
        undefined = np.zeros(len(frame), dtype=bool)
        for channel, limit in steady.max_abs_rate_per_second.items():
            rate = numeric_values(rates, roc_column(channel))
            undefined |= ~np.isfinite(rate)
            with np.errstate(invalid="ignore"):
                transient |= np.abs(rate) > limit
        split = np.full(len(frame), "steady", dtype=object)
        split[undefined] = "rate_unavailable"
        split[transient] = "transient"
        out["steady_state"] = split
    return out
