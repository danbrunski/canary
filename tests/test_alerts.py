"""Tests for the anomaly event schema + JSONL event logger (Tasks 5.1, 5.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from src.alerts.event_logger import AnomalyEventLogger, event_log_path, read_events, read_events_dir
from src.alerts.events import AnomalyEvent
from src.models.inference import SequenceInferenceResult


def _event(**overrides: object) -> AnomalyEvent:
    base: dict[str, object] = {
        "timestamp": "2026-05-13T01:30:22Z",
        "source": "lstm_autoencoder",
        "model_version": "lstm-test-1",
        "score": 0.42,
        "threshold": 0.10,
        "affected_channels": ("rpm", "maf"),
        "operating_mode": "city",
    }
    base.update(overrides)
    return AnomalyEvent(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AnomalyEvent
# ---------------------------------------------------------------------------


def test_event_round_trips_through_json() -> None:
    event = _event(session="2026-05-13T010000", latency_ms=1.7, detail={"k": [1, 2]})
    payload = json.loads(event.to_json_line())
    restored = AnomalyEvent.from_dict(payload)
    assert restored == event
    assert payload["source"] == "lstm_autoencoder"
    assert payload["affected_channels"] == ["rpm", "maf"]  # JSON has no tuples


def test_event_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        AnomalyEvent.from_dict(
            {
                "timestamp": "2026-05-13T00:00:00Z",
                "source": "isolation_forest",
                "model_version": "x",
                "score": 1.0,
                "bogus": True,
            }
        )


def test_event_rejects_unknown_source() -> None:
    with pytest.raises(ValidationError):
        AnomalyEvent.from_dict(
            {"timestamp": "t", "source": "magic_model", "model_version": "x", "score": 1.0}
        )


def test_event_from_lstm_result() -> None:
    result = SequenceInferenceResult(
        model_version="lstm-v9",
        timestamp="2026-05-13T01:00:10Z",
        session="2026-05-13T005522",
        window_start_timestamp="2026-05-13T01:00:00Z",
        window_end_timestamp="2026-05-13T01:00:10Z",
        global_score=0.55,
        global_threshold=0.12,
        channel_scores={"rpm": 0.6, "maf": 0.05},
        channel_thresholds={"rpm": 0.2, "maf": 0.2},
        anomalous_channels=("rpm",),
        is_anomaly=True,
        latency_ms=2.4,
    )
    event = AnomalyEvent.from_lstm_result(result, operating_mode="highway")
    assert event.source == "lstm_autoencoder"
    assert event.timestamp == "2026-05-13T01:00:10Z"
    assert event.model_version == "lstm-v9"
    assert event.score == 0.55 and event.threshold == 0.12
    assert event.affected_channels == ("rpm",)
    assert event.operating_mode == "highway"
    assert event.session == "2026-05-13T005522"
    assert event.detail["channel_scores"]["rpm"] == 0.6
    # serialisable
    json.loads(event.to_json_line())


def test_event_from_isolation_forest_row() -> None:
    row = pd.Series({"isolation_forest_score": 1.8, "operating_mode": "idle", "rpm": 800.0})
    event = AnomalyEvent.from_isolation_forest_row(
        row, model_version="if-v3", timestamp="2026-05-13T00:00:01Z", affected_channels=("rpm",)
    )
    assert event.source == "isolation_forest"
    assert event.score == 1.8
    assert event.operating_mode == "idle"
    assert event.threshold is None
    assert event.affected_channels == ("rpm",)


# ---------------------------------------------------------------------------
# AnomalyEventLogger
# ---------------------------------------------------------------------------


def test_logger_writes_one_line_per_event_and_flushes(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path / "alerts")
    try:
        path = logger.log(_event())
        # flushed per write — readable before close
        assert path.exists()
        assert len(path.read_text().splitlines()) == 1
        logger.log(_event(score=0.9))
        assert len(path.read_text().splitlines()) == 2
        assert logger.current_path == path
    finally:
        logger.close()
    assert path.name == "anomalies-2026-05-13.jsonl"


def test_logger_creates_directory(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path / "deep" / "nested" / "alerts")
    try:
        logger.log(_event())
    finally:
        logger.close()
    assert (tmp_path / "deep" / "nested" / "alerts").is_dir()


def test_logger_rotates_on_date_change(tmp_path: Path) -> None:
    with AnomalyEventLogger(tmp_path) as logger:
        p1 = logger.log(_event(timestamp="2026-05-13T23:59:59Z"))
        p2 = logger.log(_event(timestamp="2026-05-14T00:00:01Z"))
        p3 = logger.log(_event(timestamp="2026-05-14T00:05:00Z"))
    assert p1 != p2
    assert p2 == p3
    assert p1.name == "anomalies-2026-05-13.jsonl"
    assert p2.name == "anomalies-2026-05-14.jsonl"
    assert len(p1.read_text().splitlines()) == 1
    assert len(p2.read_text().splitlines()) == 2


def test_logger_falls_back_to_now_when_timestamp_unparseable(tmp_path: Path) -> None:
    with AnomalyEventLogger(tmp_path) as logger:
        path = logger.log(_event(timestamp="not-a-date"))
    # filename uses today's UTC date — just assert it has the expected shape
    assert path.name.startswith("anomalies-") and path.name.endswith(".jsonl")
    assert len(path.name) == len("anomalies-2026-05-13.jsonl")


def test_read_events_and_read_events_dir(tmp_path: Path) -> None:
    with AnomalyEventLogger(tmp_path) as logger:
        logger.log(_event(timestamp="2026-05-13T10:00:00Z", score=0.3))
        logger.log(_event(timestamp="2026-05-14T10:00:00Z", score=0.7))
    p13 = event_log_path(tmp_path, "2026-05-13")
    only_13 = list(read_events(p13))
    assert [e.score for e in only_13] == [0.3]
    everything = read_events_dir(tmp_path)
    assert [e.timestamp for e in everything] == ["2026-05-13T10:00:00Z", "2026-05-14T10:00:00Z"]


def test_read_events_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "anomalies-2026-05-13.jsonl"
    good = _event().to_json_line()
    # blank line, valid line, garbage, and a JSON object missing required fields
    path.write_text(f'\n{good}\nnot json at all\n{{"timestamp": "t"}}\n')
    events = list(read_events(path))
    assert len(events) == 1
    assert events[0].source == "lstm_autoencoder"
