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
from src.alerts.events import AnomalyEvent

# ---------------------------------------------------------------------------
# Fake result types — kept lightweight to avoid torch / sklearn imports here
# ---------------------------------------------------------------------------


class _FakeLSTMResult:
    """Stand-in for ``SequenceInferenceResult``; matches the attribute surface
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
        top_channels: tuple[str, ...] = (),
        reconstruction_series: dict[str, tuple[float, ...]] | None = None,
        reconstruction_overlays: dict[str, dict[str, tuple[float, ...]]] | None = None,
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
        self.top_channels = top_channels
        self.reconstruction_series = reconstruction_series or {}
        self.reconstruction_overlays = reconstruction_overlays or {}


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
    emitter.counters.skipped_vehicle_state = 3
    emitter.counters.reset()
    assert (
        emitter.counters.emitted
        == emitter.counters.suppressed_not_anomaly
        == emitter.counters.suppressed_dedupe
        == emitter.counters.skipped_vehicle_state
        == 0
    )


def test_counters_to_dict_includes_skipped_vehicle_state(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    emitter.counters.skipped_vehicle_state = 5
    assert emitter.counters.to_dict()["skipped_vehicle_state"] == 5


# ---------------------------------------------------------------------------
# Replay-boundary isolation (Phase 7.1)
# ---------------------------------------------------------------------------


def test_enter_replay_mode_gives_a_clean_slate(tmp_path: Path) -> None:
    """Entering replay must not let a live streak/dedupe/counter carry over
    into the replay run's own results."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert emitter.counters.emitted == 1

    emitter.enter_replay_mode()

    assert emitter.counters.emitted == 0
    # The live dedupe entry is gone too — an identical replay-side event at
    # the exact same timestamp isn't spuriously deduped against live.
    replay_event = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert replay_event is not None
    assert emitter.counters.emitted == 1


def test_exit_replay_mode_restores_pre_replay_counters_and_dedupe(tmp_path: Path) -> None:
    """Live picks back up exactly where it left off once replay ends — the
    replay run's own counters/dedupe don't leak back into live."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert emitter.counters.emitted == 1

    emitter.enter_replay_mode()
    for _ in range(3):
        emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T13:00:00+00:00"))
    assert emitter.counters.emitted == 1  # dedupe suppressed 2 of the 3
    emitter.exit_replay_mode()

    # Pre-replay live counters/dedupe are back, unaffected by the replay run.
    assert emitter.counters.emitted == 1
    # The live dedupe entry from before replay is still active: an
    # identical-timestamp event right after exit is still deduped.
    still_deduped = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert still_deduped is None
    assert emitter.counters.suppressed_dedupe == 1


def test_exit_replay_mode_without_enter_is_a_noop(tmp_path: Path) -> None:
    """Defensive: mirrors the writer/anomaly-logger exit_replay_mode
    best-effort contract — an unmatched exit shouldn't blow up or corrupt
    state. Dedupe must survive too: a prior regression wiped ``_last_emit``
    to ``{}`` on an unmatched exit, silently disabling live dedupe."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=1.0)
    emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert emitter.counters.emitted == 1

    emitter.exit_replay_mode()

    assert emitter.counters.emitted == 1
    # Live dedupe state must be untouched by the unmatched exit: an
    # identical-timestamp event right after it is still deduped.
    still_deduped = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-05-13T12:00:00+00:00"))
    assert still_deduped is None
    assert emitter.counters.suppressed_dedupe == 1


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


# ---------------------------------------------------------------------------
# Detector attribution payloads (Phase 7.2a)
# ---------------------------------------------------------------------------


def test_emit_lstm_event_filters_evidence_to_top_channels_when_present(
    tmp_path: Path,
) -> None:
    """When the result carries 7.2a top-channel evidence, the event payload
    is the *compact* subset — affected_channels = top, channel_scores and
    channel_thresholds filtered to top, reconstruction_series + overlays
    present, full anomalous_channels kept in detail for diagnostics."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    result = _FakeLSTMResult(
        channel_scores={"a": 0.1, "b": 5.0, "c": 0.2, "d": 0.3, "e": 0.4},
        channel_thresholds={"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0},
        anomalous_channels=("b", "e"),
        top_channels=("b", "e"),
        reconstruction_series={
            "b": (0.1, 5.0, 4.0, 3.0),
            "e": (0.0, 0.4, 0.2, 0.3),
        },
        reconstruction_overlays={
            "b": {"actual": (1.0, 2.0, 3.0, 4.0), "reconstructed": (0.9, -3.0, -1.0, 1.0)},
            "e": {"actual": (1.0, 1.0, 1.1, 0.9), "reconstructed": (1.0, 0.6, 1.0, 0.6)},
        },
    )
    event = emitter.emit_lstm(result, operating_mode="idle")
    assert event is not None
    assert event.affected_channels == ("b", "e")
    assert set(event.detail["channel_scores"]) == {"b", "e"}
    assert set(event.detail["channel_thresholds"]) == {"b", "e"}
    assert event.detail["anomalous_channels"] == ["b", "e"]
    assert set(event.detail["reconstruction_series"]) == {"b", "e"}
    assert len(event.detail["reconstruction_series"]["b"]) == 4
    overlays = event.detail["reconstruction_overlays"]
    assert set(overlays["b"]) == {"actual", "reconstructed"}
    assert len(overlays["b"]["actual"]) == 4
    # JSON round-trips through the schema.
    assert AnomalyEvent.from_dict(event.to_dict()).source == "lstm_autoencoder"


def test_emit_lstm_event_back_compat_without_top_channels(tmp_path: Path) -> None:
    """Stubs / pre-7.2a results without top_channels still produce events
    on the old contract: full channel_scores, anomalous-channel affected
    set, no reconstruction_series / reconstruction_overlays in detail."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    result = _FakeLSTMResult(
        channel_scores={"a": 0.5, "b": 5.0, "c": 0.3},
        channel_thresholds={"a": 1.0, "b": 1.0, "c": 1.0},
        anomalous_channels=("b",),
    )
    event = emitter.emit_lstm(result)
    assert event is not None
    assert event.affected_channels == ("b",)
    assert set(event.detail["channel_scores"]) == {"a", "b", "c"}  # not filtered
    assert "reconstruction_series" not in event.detail
    assert "reconstruction_overlays" not in event.detail


def test_emit_isolation_forest_row_includes_feature_contributors(
    tmp_path: Path,
) -> None:
    """7.2a IF attribution: the emitter forwards the contributors dict to
    detail and the event surfaces affected_channels alongside it."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    contributors = {"obd:RPM": 4.2, "obd:LOAD": 3.1, "obd:SPEED": 0.4}
    event = emitter.emit_isolation_forest_row(
        _scored_if_row(),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
        affected_channels=("obd:RPM", "obd:LOAD"),
        feature_contributors=contributors,
    )
    assert event is not None
    assert event.affected_channels == ("obd:RPM", "obd:LOAD")
    assert event.detail["feature_contributors"] == contributors
    assert AnomalyEvent.from_dict(event.to_dict()).detail["feature_contributors"] == contributors


def test_emit_isolation_forest_row_back_compat_without_contributors(
    tmp_path: Path,
) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    event = emitter.emit_isolation_forest_row(
        _scored_if_row(),
        model_version="if_v1",
        timestamp="2026-05-13T12:00:00+00:00",
    )
    assert event is not None
    assert "feature_contributors" not in event.detail


# ---------------------------------------------------------------------------
# DTC path (Task 1.6)
# ---------------------------------------------------------------------------


def test_emit_dtc_fires_on_a_single_call_no_persistence_gate(tmp_path: Path) -> None:
    """A DTC is already ECM-debounced — one call, one event, no warm-up
    ticks required (unlike IF/LSTM, which go through a PersistenceGate
    before their caller ever reaches ``_emit``)."""
    logger = AnomalyEventLogger(tmp_path)
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    event = emitter.emit_dtc(
        code="P0420",
        description="Catalyst System Efficiency Below Threshold (Bank 1)",
        system="Powertrain",
        status="stored",
        kind="new",
        timestamp="2026-05-13T12:00:00+00:00",
        mil_on=True,
        session="2026-05-13T120000",
    )
    assert event is not None
    assert event.source == "dtc"
    assert event.detail["code"] == "P0420"
    assert event.detail["kind"] == "new"
    assert event.detail["mil_on"] is True
    assert emitter.counters.emitted == 1
    persisted = read_events_dir(tmp_path)
    assert len(persisted) == 1


def test_emit_dtc_escalated_kind_lands_in_detail(tmp_path: Path) -> None:
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)
    event = emitter.emit_dtc(
        code="P0420",
        description="desc",
        system="Powertrain",
        status="stored",
        kind="escalated",
        timestamp="2026-05-13T12:00:00+00:00",
    )
    assert event is not None
    assert event.detail["kind"] == "escalated"
    assert event.detail["status"] == "stored"


def test_emit_dtc_dedupe_key_distinguishes_code_and_status(tmp_path: Path) -> None:
    """Two different codes (or the same code at different statuses) in the
    same instant must not suppress each other."""
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=5.0)
    ts = "2026-05-13T12:00:00+00:00"
    first = emitter.emit_dtc(
        code="P0420",
        description="d",
        system="Powertrain",
        status="pending",
        kind="new",
        timestamp=ts,
    )
    second = emitter.emit_dtc(
        code="C0510",
        description="d",
        system="Chassis",
        status="pending",
        kind="new",
        timestamp=ts,
    )
    third = emitter.emit_dtc(
        code="P0420",
        description="d",
        system="Powertrain",
        status="stored",
        kind="escalated",
        timestamp=ts,
    )
    assert first is not None
    assert second is not None
    assert third is not None
    assert emitter.counters.emitted == 3
