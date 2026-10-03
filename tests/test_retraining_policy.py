"""Tests for label-aware retraining policy (Task 6.26)."""

from __future__ import annotations

from typing import cast

import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.feedback import AnomalyFeedback, FeedbackLabel, anomaly_key
from src.models.retraining_policy import (
    apply_retraining_feedback_policy,
    build_retraining_feedback_report,
)


def _feedback(
    *,
    label: str,
    timestamp: str,
    session: str = "drive-a",
    window_start_timestamp: str | None = None,
    window_end_timestamp: str | None = None,
) -> AnomalyFeedback:
    event = {
        "timestamp": timestamp,
        "source": "isolation_forest",
        "model_version": "if_v1",
        "score": 1.0,
        "session": session,
    }
    if window_start_timestamp is not None:
        event["window_start_timestamp"] = window_start_timestamp
    if window_end_timestamp is not None:
        event["window_end_timestamp"] = window_end_timestamp
    return AnomalyFeedback(
        anomaly_key=anomaly_key(event),
        label=cast(FeedbackLabel, label),
        event=event,
        reason="operator verdict",
    )


def test_false_positive_feedback_is_kept_as_clean_baseline() -> None:
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["drive-a"] * 3,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-17T12:00:00Z", periods=3, freq="1s"),
            "obd:RPM": [700.0, 710.0, 720.0],
        }
    )
    feedback = _feedback(label="false_positive", timestamp="2026-05-17T12:00:01Z")

    filtered, report = apply_retraining_feedback_policy(
        frame,
        {feedback.anomaly_key: feedback},
    )

    assert len(filtered) == len(frame)
    assert report.clean_baseline_labels == 1
    assert report.excluded_labels == 0
    assert report.excluded_rows == 0


def test_true_positive_feedback_excludes_matching_session_window() -> None:
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["drive-a", "drive-a", "drive-a", "drive-b"],
            TIMESTAMP_COLUMN: pd.to_datetime(
                [
                    "2026-05-17T12:00:00Z",
                    "2026-05-17T12:00:01Z",
                    "2026-05-17T12:00:02Z",
                    "2026-05-17T12:00:01Z",
                ],
                utc=True,
            ),
            "obd:RPM": [700.0, 900.0, 710.0, 900.0],
        }
    )
    feedback = _feedback(
        label="true_positive",
        timestamp="2026-05-17T12:00:01Z",
        window_start_timestamp="2026-05-17T12:00:01Z",
        window_end_timestamp="2026-05-17T12:00:02Z",
    )

    filtered, report = apply_retraining_feedback_policy(
        frame,
        {feedback.anomaly_key: feedback},
    )

    assert filtered[SESSION_COLUMN].tolist() == ["drive-a", "drive-b"]
    assert filtered["obd:RPM"].tolist() == [700.0, 900.0]
    assert report.excluded_labels == 1
    assert report.excluded_rows == 2
    assert report.windows[0].action == "exclude"


def test_timestamp_only_feedback_uses_fallback_margin() -> None:
    feedback = _feedback(label="needs_review", timestamp="2026-05-17T12:00:10Z")

    report = build_retraining_feedback_report(
        {feedback.anomaly_key: feedback},
        fallback_margin_seconds=5.0,
    )

    assert report.invalid_labels == 0
    assert report.windows[0].start_timestamp == "2026-05-17T12:00:05Z"
    assert report.windows[0].end_timestamp == "2026-05-17T12:00:15Z"
