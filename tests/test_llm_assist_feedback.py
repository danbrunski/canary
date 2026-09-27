"""Tests for the enriched-label capture loop (Task S.3, case 5 model-free
core, llm-assist-and-compute-roadmap.md): a hypothesis string + one
clarifying question/answer pair attached to ``AnomalyFeedback``, flowing
into the retraining ledger the same way existing verdicts already do.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.alerts.feedback import AnomalyFeedback, FeedbackLabel, FeedbackStore, anomaly_key
from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.dashboard.api import create_app
from src.models.retraining_policy import (
    apply_retraining_feedback_policy,
    build_retraining_feedback_report,
)
from tests.configs import isolated_config

_EVENT = {
    "timestamp": "2026-07-20T10:00:00+00:00",
    "source": "lstm_autoencoder",
    "model_version": "lstm_2026",
    "score": 0.91,
    "operating_mode": "highway",
    "session": "drive-a",
}


def _feedback(
    *,
    label: str,
    timestamp: str,
    session: str = "drive-a",
    hypothesis: str | None = None,
    clarifying_question: str | None = None,
    clarifying_answer: str | None = None,
) -> AnomalyFeedback:
    event = {**_EVENT, "timestamp": timestamp, "session": session}
    return AnomalyFeedback(
        anomaly_key=anomaly_key(event),
        label=cast(FeedbackLabel, label),
        event=event,
        hypothesis=hypothesis,
        clarifying_question=clarifying_question,
        clarifying_answer=clarifying_answer,
    )


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


def test_enriched_fields_default_to_none() -> None:
    fb = _feedback(label="true_positive", timestamp=_EVENT["timestamp"])
    assert fb.hypothesis is None
    assert fb.clarifying_question is None
    assert fb.clarifying_answer is None


def test_enriched_feedback_round_trips_through_the_store(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "fb.jsonl")
    recorded = store.record(
        event=_EVENT,
        label="needs_review",
        hypothesis="possible MAF sensor drift",
        clarifying_question="were you on a dirt road or in heavy rain?",
        clarifying_answer="dirt road",
    )
    reloaded = store.get(anomaly_key(_EVENT))
    assert reloaded is not None
    assert reloaded == recorded
    assert reloaded.hypothesis == "possible MAF sensor drift"
    assert reloaded.clarifying_question == "were you on a dirt road or in heavy rain?"
    assert reloaded.clarifying_answer == "dirt road"


def test_hypothesis_alone_is_allowed_without_a_clarifying_pair() -> None:
    fb = _feedback(
        label="true_positive",
        timestamp=_EVENT["timestamp"],
        hypothesis="possible misfire",
    )
    assert fb.hypothesis == "possible misfire"
    assert fb.clarifying_question is None
    assert fb.clarifying_answer is None


def test_clarifying_question_without_an_answer_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        _feedback(
            label="true_positive",
            timestamp=_EVENT["timestamp"],
            clarifying_question="were you on a dirt road or in heavy rain?",
        )


def test_clarifying_answer_without_a_question_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        _feedback(
            label="true_positive",
            timestamp=_EVENT["timestamp"],
            clarifying_answer="dirt road",
        )


# ---------------------------------------------------------------------------
# Retrain consumption
# ---------------------------------------------------------------------------


def test_build_report_counts_enriched_labels_regardless_of_verdict() -> None:
    enriched_clean = _feedback(
        label="false_positive",
        timestamp="2026-07-20T10:00:00+00:00",
        hypothesis="A/C compressor cycling",
    )
    enriched_excluded = _feedback(
        label="true_positive",
        timestamp="2026-07-20T10:05:00+00:00",
        clarifying_question="were you accelerating hard?",
        clarifying_answer="yes",
    )
    plain = _feedback(label="ignored", timestamp="2026-07-20T10:10:00+00:00")

    report = build_retraining_feedback_report(
        {
            enriched_clean.anomaly_key: enriched_clean,
            enriched_excluded.anomaly_key: enriched_excluded,
            plain.anomaly_key: plain,
        }
    )

    assert report.total_labels == 3
    assert report.enriched_labels == 2
    # Both true_positive and ignored are excluded verdicts (see EXCLUDED_LABELS).
    assert len(report.windows) == 2
    windows_by_key = {w.anomaly_key: w for w in report.windows}
    # The Q&A-enriched item had no `hypothesis` (only clarifying Q/A) — the
    # window's hypothesis field is `None`, not synthesized from the Q/A.
    assert windows_by_key[enriched_excluded.anomaly_key].hypothesis is None
    assert windows_by_key[plain.anomaly_key].hypothesis is None


def test_excluded_window_carries_its_hypothesis() -> None:
    excluded = _feedback(
        label="true_positive",
        timestamp="2026-07-20T10:05:00+00:00",
        hypothesis="possible misfire under load",
    )
    report = build_retraining_feedback_report({excluded.anomaly_key: excluded})
    assert len(report.windows) == 1
    assert report.windows[0].hypothesis == "possible misfire under load"


def test_apply_policy_report_is_ledger_ready() -> None:
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["drive-a"] * 2,
            TIMESTAMP_COLUMN: pd.date_range("2026-07-20T10:00:00Z", periods=2, freq="1s"),
            "obd:RPM": [700.0, 710.0],
        }
    )
    enriched_excluded = _feedback(
        label="true_positive",
        timestamp="2026-07-20T10:00:00+00:00",
        hypothesis="possible misfire",
    )
    _filtered, report = apply_retraining_feedback_policy(
        frame,
        {enriched_excluded.anomaly_key: enriched_excluded},
    )
    # Exactly what scripts/retrain.py embeds as
    # TrainingRunRecord.metrics["feedback_policy"] — round-trips to JSON
    # cleanly and carries the enrichment count + the hypothesis text.
    payload = report.to_dict()
    assert payload["enriched_labels"] == 1
    assert payload["windows"][0]["hypothesis"] == "possible misfire"


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, FeedbackStore]:
    storage = tmp_path / "storage"
    (storage / "2026-07-20T100000").mkdir(parents=True)
    config = isolated_config(tmp_path, storage=storage)
    store = FeedbackStore(tmp_path / "fb.jsonl")
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(
        config,
        anomalies_dir=anomalies,
        control_token="secret",
        feedback_store=store,
    )
    return TestClient(app), store


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_post_feedback_accepts_the_enrichment_fields(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    response = client.post(
        "/control/feedback",
        headers=_auth(),
        json={
            "event": _EVENT,
            "label": "needs_review",
            "hypothesis": "possible MAF sensor drift",
            "clarifying_question": "were you on a dirt road or in heavy rain?",
            "clarifying_answer": "dirt road",
        },
    )
    assert response.status_code == 201
    body = response.json()["feedback"]
    assert body["hypothesis"] == "possible MAF sensor drift"
    assert body["clarifying_answer"] == "dirt road"
    assert store.get(anomaly_key(_EVENT)) is not None


def test_post_feedback_rejects_an_incomplete_clarifying_pair(tmp_path: Path) -> None:
    client, _store = _client(tmp_path)
    response = client.post(
        "/control/feedback",
        headers=_auth(),
        json={
            "event": _EVENT,
            "label": "needs_review",
            "clarifying_question": "were you on a dirt road or in heavy rain?",
        },
    )
    assert response.status_code == 422
