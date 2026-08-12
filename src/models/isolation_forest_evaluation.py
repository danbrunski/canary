"""Evaluation utilities for Isolation Forest synthetic anomaly runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.models.isolation_forest import (
    LABEL_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
)
from src.preprocessing.anomaly_injection import InjectionWindow, windows_to_records

DEFAULT_MIN_CONTAMINATION = 0.001
DEFAULT_MAX_CONTAMINATION = 0.5


@dataclass(frozen=True)
class ClassificationMetrics:
    """Binary anomaly classification counts and derived rates."""

    rows: int
    positives: int
    negatives: int
    predicted_positives: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def precision(self) -> float | None:
        denom = self.true_positives + self.false_positives
        if denom == 0:
            return None
        return self.true_positives / denom

    @property
    def recall(self) -> float | None:
        if self.positives == 0:
            return None
        return self.true_positives / self.positives

    @property
    def false_positive_rate(self) -> float | None:
        if self.negatives == 0:
            return None
        return self.false_positives / self.negatives

    @property
    def f1(self) -> float | None:
        precision = self.precision
        recall = self.recall
        if precision is None or recall is None or precision + recall == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "positives": self.positives,
            "negatives": self.negatives,
            "predicted_positives": self.predicted_positives,
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "true_negatives": self.true_negatives,
            "false_negatives": self.false_negatives,
            "precision": self.precision,
            "recall": self.recall,
            "false_positive_rate": self.false_positive_rate,
            "f1": self.f1,
        }


@dataclass(frozen=True)
class ScoreDistribution:
    """JSON-safe summary of anomaly scores for one population."""

    count: int
    finite_count: int
    nan_count: int
    pos_inf_count: int
    neg_inf_count: int
    minimum: float | None
    q01: float | None
    q05: float | None
    q25: float | None
    median: float | None
    q75: float | None
    q95: float | None
    q99: float | None
    maximum: float | None
    mean: float | None
    std: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "finite_count": self.finite_count,
            "nan_count": self.nan_count,
            "pos_inf_count": self.pos_inf_count,
            "neg_inf_count": self.neg_inf_count,
            "min": self.minimum,
            "q01": self.q01,
            "q05": self.q05,
            "q25": self.q25,
            "median": self.median,
            "q75": self.q75,
            "q95": self.q95,
            "q99": self.q99,
            "max": self.maximum,
            "mean": self.mean,
            "std": self.std,
        }


@dataclass(frozen=True)
class ContaminationRecommendation:
    """Per-mode contamination suggestion derived from labeled synthetic data."""

    mode: str
    recommended_contamination: float | None
    threshold: float | None
    precision: float | None
    recall: float | None
    f1: float | None
    false_positive_rate: float | None
    predicted_positive_rate: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "recommended_contamination": self.recommended_contamination,
            "threshold": self.threshold,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "false_positive_rate": self.false_positive_rate,
            "predicted_positive_rate": self.predicted_positive_rate,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ModeEvaluation:
    """Evaluation slice for one operating mode."""

    mode: str
    metrics: ClassificationMetrics
    score_distribution: ScoreDistribution
    contamination: ContaminationRecommendation

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "metrics": self.metrics.to_dict(),
            "score_distribution": self.score_distribution.to_dict(),
            "contamination": self.contamination.to_dict(),
        }


@dataclass(frozen=True)
class IsolationForestEvaluationReport:
    """Serializable report for a scored synthetic anomaly run."""

    model_version: str
    rows: int
    overall: ClassificationMetrics
    score_distribution: ScoreDistribution
    modes: Mapping[str, ModeEvaluation]
    windows: tuple[Mapping[str, object], ...]

    @property
    def recommended_per_mode_contamination(self) -> dict[str, float]:
        return {
            mode: evaluation.contamination.recommended_contamination
            for mode, evaluation in self.modes.items()
            if evaluation.contamination.recommended_contamination is not None
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "rows": self.rows,
            "overall": self.overall.to_dict(),
            "score_distribution": self.score_distribution.to_dict(),
            "modes": {mode: evaluation.to_dict() for mode, evaluation in self.modes.items()},
            "recommended_per_mode_contamination": self.recommended_per_mode_contamination,
            "windows": [dict(window) for window in self.windows],
        }


@dataclass(frozen=True)
class IsolationForestEvaluation:
    """Scored rows plus the serializable summary report."""

    scored: pd.DataFrame
    labels: pd.Series
    report: IsolationForestEvaluationReport


def evaluate_isolation_forest(
    trained: TrainedIsolationForest,
    frame: pd.DataFrame,
    labels: pd.Series | Sequence[bool],
    *,
    windows: Iterable[InjectionWindow] = (),
    min_contamination: float = DEFAULT_MIN_CONTAMINATION,
    max_contamination: float = DEFAULT_MAX_CONTAMINATION,
) -> IsolationForestEvaluation:
    """Score an injected session and summarize precision/recall by mode."""
    _validate_contamination_bounds(min_contamination, max_contamination)
    truth = _labels_for_index(labels, frame.index)
    scored = trained.score(frame)
    predictions = _predictions(scored[LABEL_COLUMN])
    scores = pd.to_numeric(scored[SCORE_COLUMN], errors="coerce")

    modes: dict[str, ModeEvaluation] = {}
    for mode, group in scored.groupby(trained.mode_column, sort=True, dropna=False):
        mode_key = str(mode)
        index = group.index
        mode_truth = truth.loc[index]
        mode_predictions = predictions.loc[index]
        mode_scores = scores.loc[index]
        modes[mode_key] = ModeEvaluation(
            mode=mode_key,
            metrics=classification_metrics(mode_truth, mode_predictions),
            score_distribution=score_distribution(mode_scores),
            contamination=recommend_contamination(
                mode_key,
                mode_scores,
                mode_truth,
                min_contamination=min_contamination,
                max_contamination=max_contamination,
            ),
        )

    report = IsolationForestEvaluationReport(
        model_version=str(trained.manifest.model_version),
        rows=len(scored),
        overall=classification_metrics(truth, predictions),
        score_distribution=score_distribution(scores),
        modes=modes,
        windows=tuple(windows_to_records(windows)),
    )
    return IsolationForestEvaluation(scored=scored, labels=truth, report=report)


def classification_metrics(
    truth: pd.Series | Sequence[bool] | np.ndarray[Any, Any],
    predictions: pd.Series | Sequence[bool] | np.ndarray[Any, Any],
) -> ClassificationMetrics:
    """Compute binary metrics for anomaly labels."""
    y_true = np.asarray(pd.Series(truth).astype(bool), dtype=bool)
    y_pred = np.asarray(pd.Series(predictions).astype(bool), dtype=bool)
    if len(y_true) != len(y_pred):
        raise ValueError("truth and predictions must have the same length")

    tp = int(np.count_nonzero(y_true & y_pred))
    fp = int(np.count_nonzero(~y_true & y_pred))
    tn = int(np.count_nonzero(~y_true & ~y_pred))
    fn = int(np.count_nonzero(y_true & ~y_pred))
    positives = int(np.count_nonzero(y_true))
    negatives = len(y_true) - positives
    predicted_positives = int(np.count_nonzero(y_pred))
    return ClassificationMetrics(
        rows=len(y_true),
        positives=positives,
        negatives=negatives,
        predicted_positives=predicted_positives,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )


def score_distribution(scores: pd.Series | Sequence[float]) -> ScoreDistribution:
    """Summarize finite scores while counting NaN/inf separately."""
    values = pd.to_numeric(pd.Series(scores), errors="coerce").to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    if len(finite) == 0:
        return ScoreDistribution(
            count=len(values),
            finite_count=0,
            nan_count=int(np.count_nonzero(np.isnan(values))),
            pos_inf_count=int(np.count_nonzero(np.isposinf(values))),
            neg_inf_count=int(np.count_nonzero(np.isneginf(values))),
            minimum=None,
            q01=None,
            q05=None,
            q25=None,
            median=None,
            q75=None,
            q95=None,
            q99=None,
            maximum=None,
            mean=None,
            std=None,
        )

    quantiles = np.quantile(finite, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
    return ScoreDistribution(
        count=len(values),
        finite_count=len(finite),
        nan_count=int(np.count_nonzero(np.isnan(values))),
        pos_inf_count=int(np.count_nonzero(np.isposinf(values))),
        neg_inf_count=int(np.count_nonzero(np.isneginf(values))),
        minimum=_safe_float(np.min(finite)),
        q01=_safe_float(quantiles[0]),
        q05=_safe_float(quantiles[1]),
        q25=_safe_float(quantiles[2]),
        median=_safe_float(quantiles[3]),
        q75=_safe_float(quantiles[4]),
        q95=_safe_float(quantiles[5]),
        q99=_safe_float(quantiles[6]),
        maximum=_safe_float(np.max(finite)),
        mean=_safe_float(np.mean(finite)),
        std=_safe_float(np.std(finite, ddof=0)),
    )


def recommend_contamination(
    mode: str,
    scores: pd.Series | Sequence[float],
    labels: pd.Series | Sequence[bool],
    *,
    min_contamination: float = DEFAULT_MIN_CONTAMINATION,
    max_contamination: float = DEFAULT_MAX_CONTAMINATION,
) -> ContaminationRecommendation:
    """Choose a per-mode contamination value from finite labeled scores.

    The selected threshold maximizes F1 on synthetic labels. Because the
    production model is fit on clean baseline data, the recommended
    contamination is the clean false-positive rate at that threshold,
    clipped to sklearn's valid range.
    """
    _validate_contamination_bounds(min_contamination, max_contamination)
    score_values = pd.to_numeric(pd.Series(scores), errors="coerce").to_numpy(dtype=float)
    label_values = np.asarray(pd.Series(labels).astype(bool), dtype=bool)
    if len(score_values) != len(label_values):
        raise ValueError("scores and labels must have the same length")

    finite = np.isfinite(score_values)
    finite_scores = score_values[finite]
    finite_labels = label_values[finite]
    if len(finite_scores) == 0:
        return _empty_recommendation(mode, "no_finite_scores")
    if not np.any(finite_labels):
        return _empty_recommendation(mode, "no_labeled_finite_anomalies")
    if np.all(finite_labels):
        return _empty_recommendation(mode, "no_finite_clean_rows")

    best: tuple[tuple[float, float, float, float, float], float, ClassificationMetrics] | None = (
        None
    )
    for threshold in sorted(np.unique(finite_scores)):
        predicted = finite_scores >= threshold
        metrics = classification_metrics(finite_labels, predicted)
        f1 = metrics.f1 if metrics.f1 is not None else 0.0
        recall = metrics.recall if metrics.recall is not None else 0.0
        precision = metrics.precision if metrics.precision is not None else 0.0
        fpr = metrics.false_positive_rate
        fpr_value = fpr if fpr is not None else 1.0
        key = (f1, recall, precision, -fpr_value, float(threshold))
        if best is None or key > best[0]:
            best = (key, float(threshold), metrics)

    if best is None:
        return _empty_recommendation(mode, "no_threshold_candidates")

    _, threshold, metrics = best
    false_positive_rate = metrics.false_positive_rate or 0.0
    recommended = min(max(false_positive_rate, min_contamination), max_contamination)
    return ContaminationRecommendation(
        mode=mode,
        recommended_contamination=_safe_float(recommended),
        threshold=_safe_float(threshold),
        precision=metrics.precision,
        recall=metrics.recall,
        f1=metrics.f1,
        false_positive_rate=metrics.false_positive_rate,
        predicted_positive_rate=_safe_float(metrics.predicted_positives / metrics.rows),
        reason="best_f1_threshold",
    )


def _predictions(labels: pd.Series) -> pd.Series:
    return labels.astype("Int64").eq(-1).fillna(False)


def _labels_for_index(labels: pd.Series | Sequence[bool], index: pd.Index) -> pd.Series:
    if isinstance(labels, pd.Series):
        aligned = labels.reindex(index)
        if aligned.isna().any():
            raise ValueError("labels must cover every frame row")
        return aligned.astype(bool)

    values = list(labels)
    if len(values) != len(index):
        raise ValueError("labels must have the same length as frame")
    return pd.Series(values, index=index, dtype=bool)


def _empty_recommendation(mode: str, reason: str) -> ContaminationRecommendation:
    return ContaminationRecommendation(
        mode=mode,
        recommended_contamination=None,
        threshold=None,
        precision=None,
        recall=None,
        f1=None,
        false_positive_rate=None,
        predicted_positive_rate=None,
        reason=reason,
    )


def _validate_contamination_bounds(min_contamination: float, max_contamination: float) -> None:
    if not 0.0 < min_contamination <= max_contamination <= 0.5:
        raise ValueError("contamination bounds must satisfy 0 < min <= max <= 0.5")


def _safe_float(value: float | np.floating[Any]) -> float | None:
    result = float(value)
    if not np.isfinite(result):
        return None
    return result
