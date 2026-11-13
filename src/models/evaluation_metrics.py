"""Budget and detection metrics for the detector evaluation.

Engine time (:func:`engine_running`, :func:`engine_hours`), the fault-caused
detection grade (:func:`fault_attributable_detection`), thresholds from the
alert budget (:func:`alert_budget_threshold`, :func:`block_maxima`), the drive
bootstrap (:func:`bootstrap_ratio_ci`) and the threshold-free :func:`vus_pr`.
Split out of :mod:`src.models.evaluation` (R27.7), which re-exports every name
here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.decode.engineering import UnscaledDecodedChannelError, physical_series
from src.models.evaluation_alerts import (
    MAX_ENGINE_STEP_SECONDS,
    PersistenceSpec,
    _verdict,
    alert_episodes,
    sustained_alerts,
)
from src.models.false_positive_benchmark import V1_ALERTS_PER_HOUR_BUDGET
from src.models.sequence_calibration import block_maxima as max_finite_per_block
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.preprocessing.segmenter import DEFAULT_SIGNAL_COLUMNS

# ---------------------------------------------------------------------------
# Engine time and detection
# ---------------------------------------------------------------------------


def engine_running(frame: pd.DataFrame) -> np.ndarray[Any, np.dtype[np.bool_]]:
    """Rows with the engine running (RPM at or above the running floor).

    RPM is the segmenter's own candidate list (``obd:RPM`` first, then the
    broadcast channel in engineering units), coalesced row-wise — so a
    capture whose profile disables Mode 01 still has engine time. Rows with
    no RPM reading at all are not engine time: an engine-hour denominator
    must not count seconds nobody measured.
    """
    rpm: np.ndarray[Any, np.dtype[np.float64]] | None = None
    for column in DEFAULT_SIGNAL_COLUMNS["rpm"]:
        if column not in frame.columns:
            continue
        try:
            values = physical_series(frame[column], column).to_numpy(dtype="float64")
        except UnscaledDecodedChannelError:
            continue
        rpm = values if rpm is None else np.where(np.isfinite(rpm), rpm, values)
    if rpm is None:
        return np.zeros(len(frame), dtype=bool)
    with np.errstate(invalid="ignore"):
        return np.asarray(np.nan_to_num(rpm, nan=0.0) >= ENGINE_RUNNING_MIN_RPM)


def engine_hours(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    running: Sequence[bool] | np.ndarray[Any, Any],
    *,
    max_step_seconds: float = MAX_ENGINE_STEP_SECONDS,
) -> float:
    """Engine-running time in hours: each running row owns the step to the
    next row, capped at ``max_step_seconds`` (a gap is not engine time)."""
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    if len(stamps) < 2:
        return 0.0
    steps = stamps.diff().dt.total_seconds().to_numpy()[1:]
    steps = np.clip(np.nan_to_num(steps, nan=0.0), 0.0, max_step_seconds)
    owned = np.asarray(running, dtype=bool)[:-1]
    return float(steps[owned].sum() / 3600.0)


DetectionStatus = Literal["detected", "missed", "withheld", "masked"]


@dataclass(frozen=True)
class DetectionOutcome:
    status: DetectionStatus
    time_to_detect_seconds: float | None = None


def fault_attributable_detection(
    timestamps: Sequence[pd.Timestamp] | pd.Series,
    *,
    faulted_alerting: Sequence[bool] | np.ndarray[Any, Any],
    clean_alerting: Sequence[bool] | np.ndarray[Any, Any],
    faulted_verdicts: Sequence[Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
    grace_seconds: float,
) -> DetectionOutcome:
    """Did the fault cause an alert, on the same rows?

    Graded only where the detector was quiet: if it alerts anywhere in
    ``[start, end + grace)`` on the identical *clean* rows, the injection is
    ``masked`` — a flooding detector cannot show a detection there, and one
    alerting near the window would otherwise be credited for its own jitter
    (the first run on real drives gave the same recall at every severity).
    Otherwise it is detected when the faulted rows alert in that span: no
    point-adjust. ``withheld`` when the detector had no verdict on any tick
    of the window: that is not a miss either. Callers report masked and
    withheld counts beside recall, never inside it.
    """
    stamps = pd.to_datetime(pd.Series(list(timestamps)), utc=True)
    horizon = end + pd.Timedelta(seconds=grace_seconds)
    in_window = ((stamps >= start) & (stamps < horizon)).to_numpy()
    scored = np.array([_verdict(v) is not None for v in faulted_verdicts], dtype=bool)
    if not bool((in_window & scored).any()):
        return DetectionOutcome("withheld")
    if bool((in_window & np.asarray(clean_alerting, dtype=bool)).any()):
        return DetectionOutcome("masked")
    caused = in_window & np.asarray(faulted_alerting, dtype=bool)
    hits = np.flatnonzero(caused)
    if hits.size == 0:
        return DetectionOutcome("missed")
    return DetectionOutcome("detected", float((stamps.iloc[int(hits[0])] - start).total_seconds()))


# ---------------------------------------------------------------------------
# Thresholds from the alert budget (A02.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreTrace:
    """One session's per-row scores for threshold setting."""

    timestamps: pd.Series
    scores: np.ndarray[Any, np.dtype[np.float64]]
    running: np.ndarray[Any, np.dtype[np.bool_]]


@dataclass(frozen=True)
class BudgetThreshold:
    """A threshold chosen so alert episodes stay within the budget."""

    threshold: float | None
    budget_per_engine_hour: float
    achieved_per_engine_hour: float | None
    engine_hours: float
    #: Independent (non-overlapping) blocks the candidates were drawn from.
    blocks: int
    #: ``False`` when the exposure cannot tell the budget from zero alerts
    #: (``engine_hours * budget < 1``): the threshold is then simply above
    #: every calibration score, and says nothing about the budget.
    resolvable: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "budget_per_engine_hour": self.budget_per_engine_hour,
            "achieved_per_engine_hour": self.achieved_per_engine_hour,
            "engine_hours": self.engine_hours,
            "blocks": self.blocks,
            "resolvable": self.resolvable,
            "note": self.note,
        }


def block_maxima(
    timestamps: pd.Series,
    scores: np.ndarray[Any, np.dtype[np.float64]],
    *,
    block_seconds: float,
) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Maximum finite score in each non-overlapping ``block_seconds`` block.

    Adjacent 1 Hz scores (and stride-1 windows) are nearly the same sample;
    block maxima are the independent draws a quantile or a budget needs.
    """
    stamps = pd.to_datetime(timestamps, utc=True).reset_index(drop=True)
    if stamps.empty:
        return np.zeros(0, dtype=np.float64)
    seconds = (stamps - stamps.iloc[0]).dt.total_seconds().to_numpy()
    blocks = np.floor(seconds / block_seconds).astype(np.int64)
    # R04.11: the "max finite per block" tail is calibration's, shared.
    return max_finite_per_block(np.asarray(scores, dtype=np.float64), blocks)


def _episode_rate(
    traces: Sequence[ScoreTrace], threshold: float, persistence: PersistenceSpec
) -> tuple[int, float]:
    count = 0
    hours = 0.0
    for trace in traces:
        verdicts = [None if not np.isfinite(s) else bool(s > threshold) for s in trace.scores]
        alerting = sustained_alerts(trace.timestamps, verdicts, persistence)
        count += len(alert_episodes(trace.timestamps, alerting, verdicts=verdicts))
        hours += engine_hours(trace.timestamps, trace.running)
    return count, hours


def alert_budget_threshold(
    traces: Sequence[ScoreTrace],
    *,
    persistence: PersistenceSpec,
    block_seconds: float,
    budget_per_engine_hour: float = V1_ALERTS_PER_HOUR_BUDGET,
) -> BudgetThreshold:
    """The lowest threshold whose alert episodes meet the budget on ``traces``.

    Candidates are the pooled per-block maxima (independent windows, A02.3),
    so the choice never rests on one session's maximum of ~36 overlapping
    windows. The budget is checked the way alerts are produced: persistence
    gate, episodes, divided by engine-hours.
    """
    hours = sum(engine_hours(t.timestamps, t.running) for t in traces)
    maxima = [block_maxima(t.timestamps, t.scores, block_seconds=block_seconds) for t in traces]
    pooled = np.unique(np.concatenate(maxima)) if maxima else np.zeros(0)
    if pooled.size == 0:
        return BudgetThreshold(
            None,
            budget_per_engine_hour,
            None,
            hours,
            0,
            False,
            "withheld: no finite score on any calibration row",
        )
    if hours <= 0.0:
        return BudgetThreshold(
            None,
            budget_per_engine_hour,
            None,
            0.0,
            int(sum(m.size for m in maxima)),
            False,
            "withheld: no engine-running time on the calibration drives",
        )
    resolvable = hours * budget_per_engine_hour >= 1.0
    # Monotone in the threshold up to persistence-gate jitter: binary search
    # the sorted candidates, then report the rate actually achieved.
    low, high = 0, int(pooled.size) - 1
    best = high
    while low <= high:
        middle = (low + high) // 2
        episodes, _ = _episode_rate(traces, float(pooled[middle]), persistence)
        if episodes / hours <= budget_per_engine_hour:
            best = middle
            high = middle - 1
        else:
            low = middle + 1
    threshold = float(pooled[best])
    episodes, _ = _episode_rate(traces, threshold, persistence)
    note = (
        f"{episodes} episode(s) in {hours:.2f} engine-h at the chosen threshold"
        if resolvable
        else (
            f"unresolvable: {hours:.2f} engine-h cannot distinguish "
            f"{budget_per_engine_hour:g}/h from zero alerts; the threshold only clears "
            "every calibration episode"
        )
    )
    return BudgetThreshold(
        threshold,
        budget_per_engine_hour,
        episodes / hours,
        hours,
        int(sum(m.size for m in maxima)),
        resolvable,
        note,
    )


# ---------------------------------------------------------------------------
# Uncertainty and threshold-free comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatioEstimate:
    """A ratio of per-drive sums with a drive-bootstrap interval."""

    estimate: float | None
    low: float | None
    high: float | None
    drives: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimate": self.estimate,
            "low": self.low,
            "high": self.high,
            "drives": self.drives,
            "note": self.note,
        }


def bootstrap_ratio_ci(
    numerators: Sequence[float],
    denominators: Sequence[float],
    *,
    n_boot: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> RatioEstimate:
    """``sum(num) / sum(den)`` with a percentile interval from resampling drives.

    Each index is one physical drive. Resampling drives — never seconds or
    windows inside a drive, which are correlated — is what makes the
    interval honest about how few independent conditions there are.
    """
    num = np.asarray(numerators, dtype=np.float64)
    den = np.asarray(denominators, dtype=np.float64)
    drives = int(num.size)
    if drives == 0 or den.sum() <= 0:
        return RatioEstimate(None, None, None, drives, "withheld: no denominator")
    estimate = float(num.sum() / den.sum())
    if drives < 2:
        return RatioEstimate(estimate, None, None, drives, "one drive: no interval")
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, drives, size=(n_boot, drives))
    boot_den = den[picks].sum(axis=1)
    valid = boot_den > 0
    ratios = num[picks].sum(axis=1)[valid] / boot_den[valid]
    if ratios.size == 0:
        return RatioEstimate(estimate, None, None, drives, "no resample had a denominator")
    low, high = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    return RatioEstimate(estimate, float(low), float(high), drives)


def _soft_labels(labels: np.ndarray[Any, np.dtype[np.bool_]], buffer: int) -> np.ndarray:
    """Range labels widened by ``buffer`` rows with a square-root ramp
    (Paparrizos et al., PVLDB 2022, the VUS definition)."""
    soft = labels.astype(np.float64).copy()
    if buffer <= 0:
        return soft
    half = buffer // 2
    n = labels.size
    edges = np.flatnonzero(np.diff(np.concatenate(([0], labels.astype(np.int8), [0]))))
    for start, stop in zip(edges[::2], edges[1::2], strict=True):
        after = np.arange(stop, min(stop + half, n))
        soft[after] = np.maximum(soft[after], np.sqrt(1 - (after - stop + 1) / buffer))
        before = np.arange(max(start - half, 0), start)
        soft[before] = np.maximum(soft[before], np.sqrt(1 - (start - before) / buffer))
    return soft


def _range_pr_auc(
    scores: np.ndarray[Any, np.dtype[np.float64]],
    labels: np.ndarray[Any, np.dtype[np.bool_]],
    buffer: int,
    thresholds: np.ndarray[Any, np.dtype[np.float64]],
) -> float:
    soft = _soft_labels(labels, buffer)
    extended = soft > 0
    edges = np.flatnonzero(np.diff(np.concatenate(([0], extended.astype(np.int8), [0]))))
    ranges = list(zip(edges[::2], edges[1::2], strict=True))
    positive = (labels.sum() + soft.sum()) / 2.0
    area = 0.0
    previous_recall = 0.0
    # Thresholds descend, so predictions only grow and recall never falls:
    # step-wise average precision, sum of (recall gained) x (precision there).
    # No interpolated (recall 0, precision 1) anchor, which would credit an
    # inverted detector with a free triangle.
    for threshold in thresholds:
        predicted = scores >= threshold
        count = int(predicted.sum())
        if count == 0:
            continue
        true_positive = float((soft * predicted).sum())
        existence = (
            sum(1 for a, b in ranges if predicted[a:b].any()) / len(ranges) if ranges else 0.0
        )
        recall = min(1.0, true_positive / positive) * existence if positive > 0 else 0.0
        if recall > previous_recall:
            area += (recall - previous_recall) * (true_positive / count)
            previous_recall = recall
    return float(area)


def vus_pr(
    scores: Sequence[float] | np.ndarray[Any, Any],
    labels: Sequence[bool] | np.ndarray[Any, Any],
    *,
    max_buffer: int = 30,
    buffer_steps: int = 6,
    n_thresholds: int = 100,
) -> float | None:
    """Volume under the range-based PR surface — threshold-free, for
    comparisons only (TSB-AD's recommended measure).

    Averages range-aware PR-AUC over label buffers ``0..max_buffer`` rows.
    Rows without a score rank lowest (the detector did not look there).
    ``None`` when there is no labelled or no unlabelled row.
    """
    values = np.asarray(scores, dtype=np.float64)
    truth = np.asarray(labels, dtype=bool)
    if truth.size == 0 or not truth.any() or truth.all():
        return None
    floor = np.nanmin(values) - 1.0 if np.isfinite(values).any() else 0.0
    values = np.where(np.isfinite(values), values, floor)
    quantiles = np.linspace(1.0, 0.0, n_thresholds)
    thresholds = np.unique(np.quantile(values, quantiles))[::-1]
    buffers = np.unique(np.linspace(0, max_buffer, buffer_steps).astype(int))
    return float(np.mean([_range_pr_auc(values, truth, int(b), thresholds) for b in buffers]))
