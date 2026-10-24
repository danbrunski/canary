"""Tests for synthetic-anomaly evaluation of Isolation Forest scores."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast

import numpy as np
import pandas as pd
import pytest

import src.models.isolation_forest_evaluation as evaluation_module
from src.columns import MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.isolation_forest import (
    CALIBRATED_ANOMALY_COLUMN,
    CALIBRATED_THRESHOLD_COLUMN,
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
)
from src.models.isolation_forest_evaluation import (
    classification_metrics,
    evaluate_isolation_forest,
    score_distribution,
)
from src.preprocessing.anomaly_injection import AnomalyInjector


@dataclass(frozen=True)
class _FakeTrainedIsolationForest:
    """Scores as ``TrainedIsolationForest.score`` shapes them: a calibrated
    threshold of 0.5, sklearn's label, and withheld rows at ``inf``."""

    scores: tuple[float, ...]
    labels: tuple[int, ...]
    withheld: tuple[bool, ...] = (False,) * 6
    threshold: float = 0.5
    mode_column: str = MODE_COLUMN
    manifest: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(model_version="iforest-eval-test")
    )

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        scores = [np.inf if w else s for s, w in zip(self.scores, self.withheld, strict=True)]
        out[SCORE_COLUMN] = scores
        out[LABEL_COLUMN] = [
            -1 if w else lab for lab, w in zip(self.labels, self.withheld, strict=True)
        ]
        out[MISSING_FEATURES_COLUMN] = list(self.withheld)
        out[CALIBRATED_THRESHOLD_COLUMN] = self.threshold
        out[CALIBRATED_ANOMALY_COLUMN] = pd.array(
            [s >= self.threshold for s in scores], dtype="boolean"
        )
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


def test_no_contamination_recommendation_is_offered() -> None:
    """A04.9: ``contamination`` only sets sklearn's ``offset_``; nothing served
    reads it, so the evaluator no longer tunes or recommends it."""
    assert not hasattr(evaluation_module, "recommend_contamination")
    assert not hasattr(evaluation_module, "ContaminationRecommendation")


def test_evaluation_grades_the_served_threshold_and_counts_withheld_rows_apart() -> None:
    """A04.9: sklearn's ``-1`` label is not a served verdict, and a withheld
    row (label ``-1``, score ``inf``) is neither a detection nor a miss."""
    frame = _frame()
    labels = pd.Series([False, True, True, False, False, True], index=frame.index)
    trained = _FakeTrainedIsolationForest(
        # served: rows 1 (0.9) and 3 (0.8) are over 0.5; sklearn says row 4 too
        scores=(0.1, 0.9, 0.4, 0.8, 0.2, 0.3),
        labels=(1, 1, 1, -1, -1, 1),
        withheld=(False, False, False, False, False, True),
    )
    report = evaluate_isolation_forest(cast(TrainedIsolationForest, trained), frame, labels).report

    assert report.withheld_rows == 1
    assert report.withheld_positive_rows == 1
    assert report.overall.rows == 5
    assert report.overall.true_positives == 1  # row 1
    assert report.overall.false_negatives == 1  # row 2 (0.4 < 0.5)
    assert report.overall.false_positives == 1  # row 3; row 4's sklearn -1 is not a verdict
    assert report.modes["city"].withheld_rows == 1
    assert report.modes["city"].threshold == pytest.approx(0.5)
    json.dumps(report.to_dict())


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

    evaluation = evaluate_isolation_forest(
        cast(TrainedIsolationForest, trained), injector.df, labels, windows=injector.windows
    )
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
        evaluate_isolation_forest(cast(TrainedIsolationForest, trained), frame, incomplete)
