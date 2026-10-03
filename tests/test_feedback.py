"""Tests for the false-positive feedback loop (Task 6.24)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_app
from src.models.feedback import (
    FeedbackStore,
    annotate_events_with_feedback,
    anomaly_key,
    feedback_to_csv,
)
from tests.configs import isolated_config

_EVENT = {
    "timestamp": "2026-05-16T10:00:00+00:00",
    "source": "lstm_autoencoder",
    "model_version": "lstm_2026",
    "score": 0.91,
    "operating_mode": "highway",
}


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


def test_anomaly_key_matches_dashboard_dedupe_tuple() -> None:
    assert anomaly_key(_EVENT) == "2026-05-16T10:00:00+00:00|lstm_autoencoder|lstm_2026"
    # Missing fields don't raise.
    assert anomaly_key({}) == "||"


def test_store_record_relabel_and_summary(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "fb.jsonl")
    assert store.labels() == {}
    store.record(event=_EVENT, label="false_positive", reason="A/C compressor cycle")
    # Relabel the same event — latest wins, history preserved.
    store.record(event=_EVENT, label="true_positive", reason="actually a misfire")
    labels = store.labels()
    assert len(labels) == 1
    fb = labels[anomaly_key(_EVENT)]
    assert fb.label == "true_positive"
    assert len(store.raw_records()) == 2
    assert store.summary() == {
        "true_positive": 1,
        "false_positive": 0,
        "ignored": 0,
        "needs_review": 0,
    }
    assert store.get(anomaly_key(_EVENT)) is not None


def test_annotate_events_with_feedback_is_pure(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "fb.jsonl")
    store.record(event=_EVENT, label="false_positive")
    other = {**_EVENT, "timestamp": "2026-05-16T11:00:00+00:00"}
    annotated = annotate_events_with_feedback([_EVENT, other], store)
    assert annotated[0]["feedback"]["label"] == "false_positive"
    assert annotated[1]["feedback"] is None
    assert "feedback" not in _EVENT  # inputs untouched


def test_feedback_to_csv(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "fb.jsonl")
    store.record(event=_EVENT, label="ignored", reason="known cold-start", labeled_by="dan")
    csv_text = feedback_to_csv(store.raw_records())
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("anomaly_key,label,reason")
    assert "ignored" in lines[1]
    assert "lstm_autoencoder" in lines[1]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, FeedbackStore]:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
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


def test_post_feedback_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post("/control/feedback", json={"event": _EVENT, "label": "false_positive"})
    assert resp.status_code == 401


def test_post_and_get_feedback(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    resp = client.post(
        "/control/feedback",
        headers=_auth(),
        json={"event": _EVENT, "label": "false_positive", "reason": "fan clutch"},
    )
    assert resp.status_code == 201
    assert resp.json()["feedback"]["label"] == "false_positive"
    assert store.get(anomaly_key(_EVENT)) is not None

    got = client.get("/control/feedback", headers=_auth())
    assert got.status_code == 200
    body = got.json()
    assert body["count"] == 1
    assert body["summary"]["false_positive"] == 1


def test_invalid_label_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/feedback",
        headers=_auth(),
        json={"event": _EVENT, "label": "totally_bogus"},
    )
    assert resp.status_code == 422


def test_anomalies_endpoint_is_annotated(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    # Write one anomaly into the daily JSONL the endpoint reads.
    from src.alerts.event_logger import AnomalyEventLogger
    from src.alerts.events import AnomalyEvent

    with AnomalyEventLogger(tmp_path / "anomalies") as logger:
        logger.log(AnomalyEvent.model_validate(_EVENT))
    store.record(event=_EVENT, label="false_positive")
    resp = client.get("/anomalies")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["feedback"]["label"] == "false_positive"


def test_feedback_503_when_not_configured(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    (storage / "s").mkdir(parents=True)
    config = isolated_config(tmp_path, storage=storage)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    resp = client.get("/control/feedback", headers=_auth())
    assert resp.status_code == 503
