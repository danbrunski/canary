"""Evaluation utilities for Isolation Forest synthetic anomaly runs.

Graded at the *served* decision (A04.9): the per-mode calibrated threshold,
through :func:`src.models.isolation_forest.served_verdicts`. The previous
version graded sklearn's ``contamination`` label — which only sets
``offset_`` and which no served decision reads — counted withheld rows (label
``-1``) as detections, and recommended contamination values tuned against
that label. A withheld row is now counted as withheld, beside the metrics and
never inside them. Held-out, drive-grouped evaluation of the served detector
is ``scripts/evaluate_detectors.py`` (A02.4); this module is the single-
session synthetic-injection check ``train_anomaly_model.py --evaluate`` runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.models.isolation_forest import (
    CALIBRATED_THRESHOLD_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
    served_verdicts,
)
from src.preprocessing.anomaly_injection import InjectionWindow, windows_to_records


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
class ModeEvaluation:
    """Evaluation slice for one operating mode, at the served threshold.

    ``metrics`` cover scored rows only; ``withheld_rows`` (and how many of
    them were labelled anomalous) are reported beside them.
    """

    mode: str
    metrics: ClassificationMetrics
    withheld_rows: int
    withheld_positive_rows: int
    threshold: float | None
    score_distribution: ScoreDistribution

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "metrics": self.metrics.to_dict(),
            "withheld_rows": self.withheld_rows,
            "withheld_positive_rows": self.withheld_positive_rows,
            "threshold": self.threshold,
            "score_distribution": self.score_distribution.to_dict(),
        }


@dataclass(frozen=True)
class IsolationForestEvaluationReport:
    """Serializable report for a scored synthetic anomaly run."""

    model_version: str
    rows: int
    overall: ClassificationMetrics
    withheld_rows: int
    withheld_positive_rows: int
    score_distribution: ScoreDistribution
    modes: Mapping[str, ModeEvaluation]
    windows: tuple[Mapping[str, object], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "rows": self.rows,
            "overall": self.overall.to_dict(),
            "withheld_rows": self.withheld_rows,
            "withheld_positive_rows": self.withheld_positive_rows,
            "score_distribution": self.score_distribution.to_dict(),
            "modes": {mode: evaluation.to_dict() for mode, evaluation in self.modes.items()},
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
) -> IsolationForestEvaluation:
    """Score an injected session and summarize precision/recall by mode, at
    the served threshold, with withheld rows counted apart."""
    truth = _labels_for_index(labels, frame.index)
    scored = trained.score(frame)
    verdicts = served_verdicts(scored)
    scored_rows = verdicts.notna()
    predictions = verdicts.where(scored_rows, False).astype(bool)
    scores = pd.to_numeric(scored[SCORE_COLUMN], errors="coerce")
    thresholds = (
        pd.to_numeric(scored[CALIBRATED_THRESHOLD_COLUMN], errors="coerce")
        if CALIBRATED_THRESHOLD_COLUMN in scored.columns
        else pd.Series(np.nan, index=scored.index)
    )

    modes: dict[str, ModeEvaluation] = {}
    for mode, group in scored.groupby(trained.mode_column, sort=True, dropna=False):
        mode_key = str(mode)
        index = group.index
        looked = scored_rows.loc[index]
        mode_thresholds = thresholds.loc[index].dropna()
        modes[mode_key] = ModeEvaluation(
            mode=mode_key,
            metrics=classification_metrics(
                truth.loc[index][looked], predictions.loc[index][looked]
            ),
            withheld_rows=int((~looked).sum()),
            withheld_positive_rows=int((truth.loc[index] & ~looked).sum()),
            threshold=None if mode_thresholds.empty else float(mode_thresholds.iloc[0]),
            score_distribution=score_distribution(scores.loc[index][looked]),
        )

    report = IsolationForestEvaluationReport(
        model_version=str(trained.manifest.model_version),
        rows=len(scored),
        overall=classification_metrics(truth[scored_rows], predictions[scored_rows]),
        withheld_rows=int((~scored_rows).sum()),
        withheld_positive_rows=int((truth & ~scored_rows).sum()),
        score_distribution=score_distribution(scores[scored_rows]),
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


def _safe_float(value: float | np.floating[Any]) -> float | None:
    result = float(value)
    if not np.isfinite(result):
        return None
    return result
