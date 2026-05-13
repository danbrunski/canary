"""Tests for the inference → AnomalyEventLogger bridge."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.alerts.emitter import (
    AnomalyEventEmitter,
    IsolationForestInferenceEngine,
)
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir

# ---------------------------------------------------------------------------
# Fake result types — kept lightweight to avoid torch / sklearn imports here
# ---------------------------------------------------------------------------


class _FakeLSTMResult:
    """Stand-in for ``LSTMInferenceResult``; matches the attribute surface
    ``AnomalyEvent.from_lstm_result`` reads. Adding fields to the real type
    will surface here as missing-attr errors, which is the failure mode we want."""

    def __init__(
        self,
        *,
        is_anomaly: bool = True,
        timestamp: str = "2026-05-13T12:00:00+00:00",
        model_version: str = "lstm_v1",
        global_score: float = 5.0,
        global_threshold: float = 3.0,
        anomalous_channels: tuple[str, ...] = ("obd:RPM",),
        channel_scores: dict[str, float] | None = None,
        channel_thresholds: dict[str, float] | None = None,
        session: str = "2026-05-13T120000",
        window_start_timestamp: str = "2026-05-13T11:59:50+00:00",
        window_end_timestamp: str = "2026-05-13T12:00:00+00:00",
        latency_ms: float = 4.5,
    ) -> None:
        self.is_anomaly = is_anomaly
        self.timestamp = timestamp
        self.model_version = model_version
        self.global_score = global_score
        self.global_threshold = global_threshold
        self.anomalous_channels = anomalous_channels
        self.channel_scores = channel_scores or {"obd:RPM": global_score}
        self.channel_thresholds = channel_thresholds or {"obd:RPM": global_threshold}
        self.session = session
        self.window_start_timestamp = window_start_timestamp
        self.window_end_timestamp = window_end_timestamp
        self.latency_ms = latency_ms


def _scored_if_row(
    *,
    label: int = -1,
    score: float = 0.7,
    mode: str = "idle",
    missing: bool = False,
) -> pd.Series:
    return pd.Series(
        {
            "isolation_forest_score": score,
            "isolation_forest_label": label,
            "isolation_forest_missing_features": missing,
            "isolation_forest_model_mode": mode,
            "operating_mode": mode,
        }
    )


# ---------------------------------------------------------------------------
# LSTM path
# ---------------------------------------------------------------------------


def test_emit_lstm_writes_event_when_is_anomaly(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_lstm(_FakeLSTMResult(), operating_mode="idle")
    assert event is not None
    assert event.source == "lstm_autoencoder"
    assert event.operating_mode == "idle"
    assert emitter.counters.emitted == 1
    persisted = read_events_dir(tmp_path)
    assert len(persisted) == 1


def test_emit_lstm_suppresses_when_not_anomaly(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_lstm(_FakeLSTMResult(is_anomaly=False))
    assert event is None
    assert emitter.counters.suppressed_not_anomaly == 1
    assert read_events_dir(tmp_path) == []


def test_emit_lstm_respects_dedupe_window(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    # 0.5s later → suppressed
    suppressed = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00.500000+00:00"))
    assert suppressed is None
    assert emitter.counters.suppressed_dedupe == 1
    # 1.5s later → emitted
    emitted = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:01.500000+00:00"))
    assert emitted is not None
    assert emitter.counters.emitted == 2


def test_dedupe_key_separates_sources(tmp_path: Path) -> None:
    """A dedupe block on one source mustn't block another with the same timestamp."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    ts = "2026-05-13T12:00:00+00:00"
    emitter.emit_lstm(_FakeLSTMResult(timestamp=ts))
    if_event = emitter.emit_isolation_forest_row(
        _scored_if_row(),
        model_version="if_v1",
        timestamp=ts,
    )
    assert if_event is not None  # different source → not deduped
    assert emitter.counters.emitted == 2


def test_dedupe_zero_disables(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    for _ in range(5):
        emitter.emit_lstm(_FakeLSTMResult())
    assert emitter.counters.emitted == 5
    assert emitter.counters.suppressed_dedupe == 0


# ---------------------------------------------------------------------------
# Isolation Forest path
# ---------------------------------------------------------------------------


def test_emit_if_row_writes_event_when_label_outlier(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_isolation_forest_row(
        _scored_if_row(label=-1, score=0.6, mode="city"),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
        affected_channels=("decoded:ECM_RPM",),
        session="2026-05-13T120000",
    )
    assert event is not None
    assert event.source == "isolation_forest"
    assert event.score == 0.6
    assert event.operating_mode == "city"
    assert event.affected_channels == ("decoded:ECM_RPM",)
    assert event.detail.get("missing_features") is None


def test_emit_if_row_suppresses_when_inlier(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_isolation_forest_row(
        _scored_if_row(label=1, score=-0.2),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
    )
    assert event is None
    assert emitter.counters.suppressed_not_anomaly == 1


def test_emit_if_row_fires_on_missing_features_flag(tmp_path: Path) -> None:
    """Dropout (missing features) is a v1 anomaly per IF's score()."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_isolation_forest_row(
        _scored_if_row(label=-1, missing=True, score=math.inf),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
    )
    assert event is not None
    # tagged so the dashboard can render this differently from "real" outlier
    assert event.detail.get("missing_features") is True


def test_emit_if_row_fires_on_missing_features_even_if_label_none(tmp_path: Path) -> None:
    """The score() path leaves label as <NA> for dropout rows in some cases."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    row = _scored_if_row()
    row["isolation_forest_label"] = pd.NA
    row["isolation_forest_missing_features"] = True
    event = emitter.emit_isolation_forest_row(
        row, model_version="if_v1", timestamp="2026-05-13T12:00:00+00:00"
    )
    assert event is not None


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def test_counters_reset_zeros_all(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    emitter.emit_lstm(_FakeLSTMResult())
    emitter.emit_lstm(_FakeLSTMResult(is_anomaly=False))
    assert emitter.counters.emitted == 1
    emitter.counters.reset()
    assert (
        emitter.counters.emitted
        == emitter.counters.suppressed_not_anomaly
        == emitter.counters.suppressed_dedupe
        == 0
    )


# ---------------------------------------------------------------------------
# Bad timestamps
# ---------------------------------------------------------------------------


def test_emitter_rejects_unparseable_timestamp(tmp_path: Path) -> None:
    """Dedupe relies on parsing the timestamp; garbage in → loud failure."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    with pytest.raises(ValueError):
        emitter.emit_lstm(_FakeLSTMResult(timestamp="not-a-timestamp"))


def test_emitter_accepts_z_suffix_timestamps(tmp_path: Path) -> None:
    """The LSTM inference path produces ...Z timestamps; must be accepted."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    event = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00Z"))
    assert event is not None
    suppressed = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00.500000Z"))
    assert suppressed is None  # within the 1-second dedupe window


# ---------------------------------------------------------------------------
# IsolationForestInferenceEngine — streaming wrapper
# ---------------------------------------------------------------------------


class _FakeTrained:
    """Stand-in for TrainedIsolationForest; satisfies the attribute surface
    IsolationForestInferenceEngine reads. The score() returns a frame
    indistinguishable from the real one for one-row inputs."""

    def __init__(self, label: int = -1) -> None:
        self.feature_columns = ("obd:RPM", "obd:SPEED")
        self.mode_column = "operating_mode"
        self.manifest = type("M", (), {"model_version": "if_test_v1"})()
        self._label = label

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = frame.copy()
        out["isolation_forest_score"] = 0.5
        out["isolation_forest_label"] = self._label
        out["isolation_forest_model_mode"] = "idle"
        out["isolation_forest_missing_features"] = False
        return out


def test_if_inference_engine_observe_returns_scored_row() -> None:
    engine = IsolationForestInferenceEngine(_FakeTrained(label=-1))
    row = engine.observe({"obd:RPM": 700.0, "obd:SPEED": 0.0, "operating_mode": "idle"})
    assert int(row["isolation_forest_label"]) == -1
    assert row["isolation_forest_score"] == 0.5


def test_if_inference_engine_accepts_pandas_series() -> None:
    engine = IsolationForestInferenceEngine(_FakeTrained(label=1))
    row = engine.observe(pd.Series({"obd:RPM": 700.0, "obd:SPEED": 0.0, "operating_mode": "idle"}))
    assert int(row["isolation_forest_label"]) == 1


def test_engine_plus_emitter_end_to_end(tmp_path: Path) -> None:
    """The intended daemon shape: engine.observe → emitter.emit → logger."""
    engine = IsolationForestInferenceEngine(_FakeTrained(label=-1))
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    scored = engine.observe({"obd:RPM": 700.0, "obd:SPEED": 0.0, "operating_mode": "idle"})
    event = emitter.emit_isolation_forest_row(
        scored,
        model_version=engine.model_version,
        timestamp="2026-05-13T12:00:00+00:00",
        session="2026-05-13T120000",
    )
    assert event is not None
    persisted = read_events_dir(tmp_path)
    assert len(persisted) == 1
    assert persisted[0].source == "isolation_forest"
    assert persisted[0].model_version == "if_test_v1"


def test_engine_does_not_emit_inlier_rows(tmp_path: Path) -> None:
    engine = IsolationForestInferenceEngine(_FakeTrained(label=1))
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    scored = engine.observe({"obd:RPM": 700.0, "obd:SPEED": 0.0, "operating_mode": "idle"})
    event = emitter.emit_isolation_forest_row(
        scored, model_version=engine.model_version, timestamp="2026-05-13T12:00:00+00:00"
    )
    assert event is None
    assert read_events_dir(tmp_path) == []


def _unused_for_silencing_a_helper_import(_: Any) -> None:
    """Keep `Any` referenced so the import isn't flagged on edits."""
