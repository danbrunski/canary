"""Tests for synthetic-anomaly evaluation of Isolation Forest scores."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.models.isolation_forest import LABEL_COLUMN, SCORE_COLUMN
from src.models.isolation_forest_evaluation import (
    classification_metrics,
    evaluate_isolation_forest,
    recommend_contamination,
    score_distribution,
)
from src.preprocessing.anomaly_injection import AnomalyInjector
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN


@dataclass(frozen=True)
class _FakeTrainedIsolationForest:
    scores: tuple[float, ...]
    labels: tuple[int, ...]
    mode_column: str = MODE_COLUMN
    manifest: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(model_version="iforest-eval-test")
    )

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out[SCORE_COLUMN] = list(self.scores)
        out[LABEL_COLUMN] = list(self.labels)
        return out


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["eval"] * 6,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-11T00:00:00Z", periods=6, freq="1s"),
            MODE_COLUMN: ["idle", "idle", "idle", "city", "city", "city"],
            "obd:RPM": [800.0, 4000.0, 4100.0, 2100.0, 2200.0, 5000.0],
            "obd:SPEED": [0.0, 0.0, 0.0, 35.0, 36.0, 120.0],
        }
    )


def test_classification_metrics_computes_counts_and_rates() -> None:
    metrics = classification_metrics(
        truth=[False, True, True, False],
        predictions=[False, True, False, True],
    )

    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.true_negatives == 1
    assert metrics.false_negatives == 1
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == pytest.approx(0.5)
    assert metrics.false_positive_rate == pytest.approx(0.5)
    assert metrics.f1 == pytest.approx(0.5)


def test_score_distribution_counts_nonfinite_scores_separately() -> None:
    distribution = score_distribution([0.1, 0.2, np.inf, np.nan, -np.inf])

    assert distribution.count == 5
    assert distribution.finite_count == 2
    assert distribution.nan_count == 1
    assert distribution.pos_inf_count == 1
    assert distribution.neg_inf_count == 1
    assert distribution.minimum == pytest.approx(0.1)
    assert distribution.maximum == pytest.approx(0.2)
    assert distribution.median == pytest.approx(0.15)


def test_recommend_contamination_chooses_best_f1_threshold() -> None:
    recommendation = recommend_contamination(
        "idle",
        scores=[0.1, 0.2, 0.8, 0.9],
        labels=[False, False, True, True],
    )

    assert recommendation.reason == "best_f1_threshold"
    assert recommendation.threshold == pytest.approx(0.8)
    assert recommendation.precision == pytest.approx(1.0)
    assert recommendation.recall == pytest.approx(1.0)
    assert recommendation.f1 == pytest.approx(1.0)
    assert recommendation.false_positive_rate == pytest.approx(0.0)
    assert recommendation.recommended_contamination == pytest.approx(0.001)


def test_recommend_contamination_reports_untunable_modes() -> None:
    recommendation = recommend_contamination(
        "highway",
        scores=[0.1, 0.2, 0.3],
        labels=[False, False, False],
    )

    assert recommendation.recommended_contamination is None
    assert recommendation.reason == "no_labeled_finite_anomalies"


def test_evaluate_isolation_forest_reports_overall_and_per_mode_results() -> None:
    frame = _frame()
    injector = AnomalyInjector(frame)
    injector.spike(
        channel="obd:RPM",
        start="2026-05-11T00:00:01Z",
        end="2026-05-11T00:00:03Z",
        value=4200.0,
    )
    labels = injector.labels()
    # Ground truth positives: idle rows 1 and 2. Predictions catch row 1,
    # miss row 2, and create one false positive in city row 3.
    trained = _FakeTrainedIsolationForest(
        scores=(0.1, 0.9, 0.4, 0.8, 0.2, 0.3),
        labels=(1, -1, 1, -1, 1, 1),
    )

    evaluation = evaluate_isolation_forest(trained, injector.df, labels, windows=injector.windows)
    report = evaluation.report

    assert report.model_version == "iforest-eval-test"
    assert report.overall.true_positives == 1
    assert report.overall.false_positives == 1
    assert report.overall.false_negatives == 1
    assert report.overall.precision == pytest.approx(0.5)
    assert report.overall.recall == pytest.approx(0.5)

    assert set(report.modes) == {"city", "idle"}
    assert report.modes["idle"].metrics.positives == 2
    assert report.modes["idle"].metrics.recall == pytest.approx(0.5)
    assert report.modes["city"].metrics.false_positives == 1
    assert report.modes["city"].contamination.reason == "no_labeled_finite_anomalies"
    assert report.modes["idle"].contamination.recommended_contamination == pytest.approx(0.001)
    assert report.recommended_per_mode_contamination == {"idle": pytest.approx(0.001)}
    assert len(report.windows) == 1

    # The report is intended to be persisted as plain JSON.
    json.dumps(report.to_dict())


def test_evaluate_isolation_forest_requires_label_coverage() -> None:
    frame = _frame()
    trained = _FakeTrainedIsolationForest(
        scores=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6),
        labels=(1, 1, 1, 1, 1, 1),
    )
    incomplete = pd.Series([False], index=[frame.index[0]])

    with pytest.raises(ValueError, match="labels must cover"):
        evaluate_isolation_forest(trained, frame, incomplete)
