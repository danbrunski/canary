"""A06.5 — per-detector roles: ``alert`` (default) vs ``evidence``.

An ``evidence``-role detector still scores and stores its events, flagged
non-alerting, and still counts toward fusion agreement — but it can never
raise an alert on its own: no ntfy push, no dashboard "active" state, and a
fused event needs at least one alert-role source among its agreeing voices
(fusion stays the pre-made ``agreement >= 2`` gate, OVERNIGHT-QUEUE.md).
The default config leaves both detectors at ``alert``, so behaviour is
unchanged until the operator decides A06.1/A06.2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
from src.alerts.events import AnomalyEvent
from src.alerts.fusion import FusionScorer, isolation_forest_source, lstm_source
from src.config import CanaryConfig, DetectorRolesConfig, FusionConfig

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeLSTMResult:
    """Attribute surface ``AnomalyEvent.from_lstm_result`` reads."""

    def __init__(self, *, timestamp: str = "2026-09-23T12:00:00+00:00") -> None:
        self.is_anomaly = True
        self.timestamp = timestamp
        self.model_version = "lstm_v1"
        self.global_score = 5.0
        self.global_threshold = 3.0
        self.anomalous_channels = ("obd:RPM",)
        self.channel_scores = {"obd:RPM": 5.0}
        self.channel_thresholds = {"obd:RPM": 3.0}
        self.session = "2026-09-23T120000"
        self.window_start_timestamp = "2026-09-23T11:59:30+00:00"
        self.window_end_timestamp = timestamp
        self.latency_ms = 3.0
        self.top_channels = ()
        self.reconstruction_series: dict[str, Any] = {}
        self.reconstruction_overlays: dict[str, Any] = {}


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, *, priority: str = "default") -> None:
        self.calls.append((title, message))


def _if_row(score: float = 0.9) -> pd.Series:
    return pd.Series(
        {
            "isolation_forest_score": score,
            "isolation_forest_label": -1,
            "isolation_forest_missing_features": False,
            "isolation_forest_model_mode": "idle",
            "operating_mode": "idle",
        }
    )


def _emitter(
    tmp_path: Path, roles: dict[str, Any] | None, notifier: _FakeNotifier
) -> AnomalyEventEmitter:
    return AnomalyEventEmitter(
        AnomalyEventLogger(tmp_path),
        min_seconds_between_events=0,
        notifier=notifier,  # type: ignore[arg-type]
        notify_min_severity=0.0,
        notify_max_pushes_per_session=100,
        roles=roles,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_roles_default_to_the_operators_decision() -> None:
    """A06.1/A06.2 (operator, 2026-09-23): neither detector alerts on its own."""
    roles = DetectorRolesConfig()
    assert roles.isolation_forest == "evidence"
    assert roles.sequence == "evidence"
    assert roles.by_source() == {
        "isolation_forest": "evidence",
        "lstm_autoencoder": "evidence",
    }


@pytest.mark.parametrize(
    "example", ["canary.example.yaml", "canary.pi.example.yaml", "canary.dev.example.yaml"]
)
def test_shipped_example_configs_carry_the_operators_roles(example: str) -> None:
    config = CanaryConfig.from_yaml(REPO_ROOT / example)
    assert config.models.roles.by_source() == {
        "isolation_forest": "evidence",
        "lstm_autoencoder": "evidence",
    }


def test_sequence_role_names_the_event_source_for_either_backbone() -> None:
    """The TCN and the LSTM fill the same slot and publish the same
    ``lstm_autoencoder`` event source, so one ``sequence`` role covers both."""
    roles = DetectorRolesConfig(sequence="evidence")
    assert roles.by_source()["lstm_autoencoder"] == "evidence"


def test_unknown_role_or_detector_is_refused() -> None:
    with pytest.raises(ValidationError):
        DetectorRolesConfig(isolation_forest="evidance")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DetectorRolesConfig.model_validate({"lstm": "evidence"})


# ---------------------------------------------------------------------------
# Emitter: stored, flagged, never pushed
# ---------------------------------------------------------------------------


def test_default_role_events_are_alerts_and_push(tmp_path: Path) -> None:
    notifier = _FakeNotifier()
    emitter = _emitter(tmp_path, None, notifier)
    event = emitter.emit_lstm(_FakeLSTMResult())
    assert event is not None and event.role == "alert"
    assert emitter.counters.emitted == 1
    assert emitter.counters.emitted_evidence == 0
    assert len(notifier.calls) == 1
    assert read_events_dir(tmp_path)[0].role == "alert"


def test_evidence_role_event_is_stored_flagged_and_never_pushed(tmp_path: Path) -> None:
    notifier = _FakeNotifier()
    emitter = _emitter(tmp_path, {"lstm_autoencoder": "evidence"}, notifier)
    event = emitter.emit_lstm(_FakeLSTMResult())
    assert event is not None
    assert event.role == "evidence"
    assert notifier.calls == []  # an evidence event can never reach ntfy
    assert emitter.counters.emitted == 0  # not an alert
    assert emitter.counters.emitted_evidence == 1
    persisted = read_events_dir(tmp_path)
    assert len(persisted) == 1 and persisted[0].role == "evidence"
    assert emitter.counters.to_dict()["emitted_evidence"] == 1


def test_evidence_role_is_per_detector(tmp_path: Path) -> None:
    notifier = _FakeNotifier()
    emitter = _emitter(tmp_path, {"isolation_forest": "evidence"}, notifier)
    if_event = emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-09-23T12:00:00+00:00"
    )
    lstm_event = emitter.emit_lstm(_FakeLSTMResult(timestamp="2026-09-23T12:00:01+00:00"))
    assert if_event is not None and if_event.role == "evidence"
    assert lstm_event is not None and lstm_event.role == "alert"
    assert [title for title, _ in notifier.calls] == ["CANary: lstm_autoencoder anomaly"]
    assert emitter.counters.emitted == 1
    assert emitter.counters.emitted_evidence == 1


def test_an_event_already_marked_evidence_is_never_promoted(tmp_path: Path) -> None:
    """The stricter of the event's own role and the configured one wins."""
    notifier = _FakeNotifier()
    emitter = _emitter(tmp_path, None, notifier)
    event = AnomalyEvent.from_lstm_result(_FakeLSTMResult()).model_copy(update={"role": "evidence"})
    emitted = emitter._emit(event)
    assert emitted is not None and emitted.role == "evidence"
    assert notifier.calls == []


def test_event_role_round_trips_and_old_records_load_as_alert() -> None:
    event = AnomalyEvent.from_lstm_result(_FakeLSTMResult()).model_copy(update={"role": "evidence"})
    assert AnomalyEvent.from_dict(event.to_dict()).role == "evidence"
    legacy = event.to_dict()
    del legacy["role"]  # a record written before A06.5
    assert AnomalyEvent.from_dict(legacy).role == "alert"


# ---------------------------------------------------------------------------
# Fusion: evidence counts toward agreement but cannot alert on its own
# ---------------------------------------------------------------------------


def _both_firing() -> list[Any]:
    return [lstm_source(0.10, 0.05), isolation_forest_source(0.0, -0.0645, scale=0.05)]


def test_default_fusion_is_unchanged() -> None:
    fused = FusionScorer(FusionConfig()).fuse(_both_firing(), timestamp="t", operating_mode=None)
    assert fused.is_anomaly and fused.agreement == 2
    assert fused.severity == 1.0
    assert {name: s["role"] for name, s in fused.to_detail()["sources"].items()} == {
        "lstm_autoencoder": "alert",
        "isolation_forest": "alert",
    }


def test_evidence_source_still_counts_toward_agreement() -> None:
    fs = FusionScorer(FusionConfig(), roles={"isolation_forest": "evidence"})
    fused = fs.fuse(_both_firing(), timestamp="t", operating_mode=None)
    # IF (evidence) + sequence (alert) → two independent voices, one alerting.
    assert fused.agreement == 2
    assert fused.is_anomaly
    # Evidence adds the agreement boost but not its own magnitude: the base
    # is the alert-role sequence source's 1.0 here anyway.
    assert fused.to_detail()["sources"]["isolation_forest"]["role"] == "evidence"


def test_evidence_sets_no_magnitude_only_the_agreement_boost() -> None:
    fs = FusionScorer(
        FusionConfig(agreement_boost=0.25, severity_threshold=0.5),
        roles={"isolation_forest": "evidence"},
    )
    fused = fs.fuse(
        # sequence at its boundary (0.5); IF far over (~1.0) but evidence.
        [lstm_source(0.05, 0.05), isolation_forest_source(0.05, -0.0645, scale=0.05)],
        timestamp="t",
        operating_mode=None,
    )
    assert fused.severity == pytest.approx(0.75)  # 0.5 base + 0.25 boost
    assert fused.is_anomaly


def test_two_evidence_sources_agreeing_never_alert() -> None:
    fs = FusionScorer(
        FusionConfig(), roles={"isolation_forest": "evidence", "lstm_autoencoder": "evidence"}
    )
    fused = fs.fuse(_both_firing(), timestamp="t", operating_mode=None)
    assert fused.agreement == 2  # the agreement is still recorded...
    assert set(fused.contributing) == {"lstm_autoencoder", "isolation_forest"}
    assert not fused.is_anomaly  # ...but nothing alert-role fired
    assert fused.severity == 0.0


def test_evidence_plus_behavior_residual_alerts() -> None:
    fs = FusionScorer(
        FusionConfig(), roles={"isolation_forest": "evidence", "lstm_autoencoder": "evidence"}
    )
    fused = fs.fuse(
        [
            isolation_forest_source(0.0, -0.0645, scale=0.05),
            fs.behavior_source("behavior:coolant", "warning", channel="obd:COOLANT_TEMP"),
        ],
        timestamp="t",
        operating_mode="city",
    )
    assert fused.agreement == 2 and fused.is_anomaly


def test_single_evidence_source_is_not_an_anomaly_even_at_min_agreement_one() -> None:
    fs = FusionScorer(FusionConfig(min_agreement=1), roles={"isolation_forest": "evidence"})
    fused = fs.fuse(
        [isolation_forest_source(0.0, -0.0645, scale=0.05)], timestamp="t", operating_mode=None
    )
    assert fused.agreement == 1
    assert not fused.is_anomaly


# ---------------------------------------------------------------------------
# Daemon wiring: config roles reach the emitter and the pipeline's fusion
# ---------------------------------------------------------------------------


def _daemon_yaml(tmp_path: Path, *, sequence_role: str) -> str:
    return f"""
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""
hardware:
  elm327_port: /dev/ttyUSB0
  can:
    interface: virtual
    channel: vcan0
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {tmp_path / "storage"}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    idle:
      rpm_max: 900
      speed_max: 0
models:
  out_of_process_feature_build: false
  roles:
    isolation_forest: alert
    sequence: {sequence_role}
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
alerts:
  min_consecutive_ticks: 1
  persistence_window_ticks: 1
  min_seconds_between_events: 0
dashboard:
  host: 127.0.0.1
  port: 0
daemon:
  anomalies_dir: {tmp_path / "anomalies"}
  models_dir: {tmp_path / "models"}
  model_poll_seconds: 0.05
  retention_sweep_seconds: 0.05
  broadcaster_queue_size: 16
  catalog_db: {tmp_path / "control" / "catalog.db"}
"""


def _daemon_config(tmp_path: Path, *, sequence_role: str) -> CanaryConfig:
    path = tmp_path / "canary.yaml"
    path.write_text(_daemon_yaml(tmp_path, sequence_role=sequence_role))
    return CanaryConfig.from_yaml(path)


async def test_daemon_sequence_in_evidence_role_stores_but_fused_needs_an_alert_source(
    tmp_path: Path,
) -> None:
    from src.daemon import build_components, build_live_feature_source
    from src.daemon.inference import LiveInferencePipeline, SequenceDetector, TickContext
    from src.daemon.inference_metrics import InferenceTimingRecorder

    config = _daemon_config(tmp_path, sequence_role="evidence")
    components = build_components(config)
    anomalies_dir = config.daemon.anomalies_dir
    ctx = TickContext(
        now=datetime(2026, 9, 23, 12, tzinfo=UTC),
        operating_mode="idle",
        timestamp_str="2026-09-23T12:00:00+00:00",
        scoring_allowed=True,
    )

    # Per-source path: the sequence event is stored, flagged, not an alert.
    sources = SequenceDetector(components).apply(_FakeLSTMResult(), ctx)
    stored = read_events_dir(anomalies_dir)
    assert [(e.source, e.role) for e in stored] == [("lstm_autoencoder", "evidence")]
    assert components.emitter.counters.emitted == 0
    assert components.emitter.counters.emitted_evidence == 1
    assert sources and sources[0].fired  # still a firing fusion voice

    source = build_live_feature_source(components)
    pipeline = LiveInferencePipeline(
        source, components, interval_seconds=1.0, recorder=InferenceTimingRecorder()
    )
    try:
        # Sequence (evidence) agreeing with the IF (alert role, the default).
        pipeline._fuse([*sources, isolation_forest_source(0.0, -0.0645, scale=0.05)], ctx)
        fused = [e for e in read_events_dir(anomalies_dir) if e.source == "fused"]
        assert len(fused) == 1
        assert fused[0].role == "alert"
        assert fused[0].detail["sources"]["lstm_autoencoder"]["role"] == "evidence"
        assert fused[0].detail["agreement"] == 2
    finally:
        await pipeline.close(timeout_seconds=1.0)


async def test_daemon_evidence_only_agreement_raises_no_fused_event(tmp_path: Path) -> None:
    from src.daemon import build_components, build_live_feature_source
    from src.daemon.inference import LiveInferencePipeline, TickContext
    from src.daemon.inference_metrics import InferenceTimingRecorder

    config = _daemon_config(tmp_path, sequence_role="evidence")
    config.models.roles.isolation_forest = "evidence"
    components = build_components(config)
    pipeline = LiveInferencePipeline(
        build_live_feature_source(components),
        components,
        interval_seconds=1.0,
        recorder=InferenceTimingRecorder(),
    )
    try:
        pipeline._fuse(
            _both_firing(),
            TickContext(
                now=datetime(2026, 9, 23, 12, tzinfo=UTC),
                operating_mode="idle",
                timestamp_str="2026-09-23T12:00:00+00:00",
                scoring_allowed=True,
            ),
        )
    finally:
        await pipeline.close(timeout_seconds=1.0)
    assert [e for e in read_events_dir(config.daemon.anomalies_dir) if e.source == "fused"] == []
    assert components.emitter.counters.suppressed_not_anomaly == 1
