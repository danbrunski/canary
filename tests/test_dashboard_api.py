"""Tests for the dashboard REST endpoints (Task 5.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.config import CanaryConfig
from src.dashboard.api import (
    StatusSnapshot,
    build_status,
    create_app,
    echo_config,
    read_anomalies_since,
    read_latest_telemetry_rows,
)
from src.storage.parquet_writer import TELEMETRY_SCHEMA

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config_yaml(storage_path: Path) -> str:
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
  storage_path: {storage_path}
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
  isolation_forest:
    enabled: true
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: true
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 8000
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    return CanaryConfig.from_yaml(cfg_path)


@pytest.fixture
def anomalies_dir(tmp_path: Path) -> Path:
    return tmp_path / "anomalies"


def _make_session_with_parquet(storage_root: Path, name: str, rows: int = 3) -> Path:
    """Drop a minimal Parquet chunk into ``storage_root/name/``."""
    session = storage_root / name
    session.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        [
            {
                "timestamp": pa.scalar(
                    1_778_000_000_000_000 + i * 1_000_000,
                    type=pa.timestamp("us", tz="UTC"),
                ).as_py(),
                "source": "obd",
                "channel": "RPM",
                "value_number": float(700 + i),
                "value_text": None,
                "value_bool": None,
                "unit": "rpm",
                "payload": None,
                "metadata_json": None,
            }
            for i in range(rows)
        ],
        schema=TELEMETRY_SCHEMA,
    )
    pq.write_table(table, session / "telemetry-00000.parquet")
    return session


# ---------------------------------------------------------------------------
# build_status — unit
# ---------------------------------------------------------------------------


def test_build_status_no_sessions_no_models(config: CanaryConfig) -> None:
    snap = build_status(config)
    assert snap.latest_session is None
    assert snap.latest_session_path is None
    assert snap.model_versions == {}
    assert "Nissan Xterra" in snap.vehicle
    assert snap.free_mb > 0
    # No emitter passed → counters absent (None)
    assert snap.emitter_counters is None


def test_build_status_surfaces_emitter_counters(config: CanaryConfig, tmp_path: Path) -> None:
    """When the daemon passes the emitter, /status carries its counters."""
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger

    logger = AnomalyEventLogger(tmp_path / "anomalies")
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    # Hand-modify the counters to a non-zero state — emit_lstm would otherwise
    # require a result fixture, and the build_status path is what we're testing.
    emitter.counters.emitted = 4
    emitter.counters.suppressed_not_anomaly = 12
    emitter.counters.suppressed_dedupe = 1

    snap = build_status(config, emitter=emitter)
    assert snap.emitter_counters == {
        "emitted": 4,
        "suppressed_not_anomaly": 12,
        "suppressed_dedupe": 1,
    }


def test_create_app_mounts_static_when_dist_exists(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    """When the SPA has been built, GET / returns the index.html."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>canary-test</title>")
    app = create_app(config, anomalies_dir=anomalies_dir, static_dir=static_dir)
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "canary-test" in response.text


def test_create_app_skips_static_when_dist_missing(
    config: CanaryConfig, anomalies_dir: Path, tmp_path: Path
) -> None:
    """Until `npm run build`, /  returns 404 — the API routes still work."""
    app = create_app(config, anomalies_dir=anomalies_dir, static_dir=tmp_path / "absent")
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 404
    # /status still works
    assert client.get("/status").status_code == 200


def test_create_app_dev_cors_allows_vite_origin(config: CanaryConfig, anomalies_dir: Path) -> None:
    """``enable_dev_cors=True`` permits the Vite dev origin so the frontend
    can call the backend directly from localhost:5173."""
    app = create_app(config, anomalies_dir=anomalies_dir, enable_dev_cors=True)
    client = TestClient(app)
    response = client.get("/status", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_status_endpoint_includes_emitter_counters(config: CanaryConfig, tmp_path: Path) -> None:
    """End-to-end through TestClient: the /status response carries the counters dict."""
    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger

    logger = AnomalyEventLogger(tmp_path / "anomalies-status")
    emitter = AnomalyEventEmitter(logger, min_seconds_between_events=0)
    emitter.counters.emitted = 2
    app = create_app(config, anomalies_dir=tmp_path / "anomalies-status", emitter=emitter)
    client = TestClient(app)
    response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["emitter_counters"]["emitted"] == 2


def test_build_status_with_session_and_model_manifest(config: CanaryConfig, tmp_path: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000")
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T130000")

    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "isolation_forest.manifest.json").write_text(
        json.dumps({"version": "2026-05-13"})
    )

    snap = build_status(config, models_dir=models_dir)
    # newest session wins
    assert snap.latest_session == "2026-05-13T130000"
    assert snap.model_versions == {"isolation_forest": "2026-05-13"}


def test_status_snapshot_to_dict_rounds_free_mb() -> None:
    snap = StatusSnapshot(
        storage_path="/x",
        free_mb=1234.56789,
        latest_session=None,
        latest_session_path=None,
        model_versions={},
        vehicle="Test Car",
    )
    assert snap.to_dict()["free_mb"] == 1234.6


def test_build_status_ignores_unreadable_manifest(config: CanaryConfig, tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "broken.manifest.json").write_text("{not json")
    snap = build_status(config, models_dir=models_dir)
    assert snap.model_versions == {"broken": None}


# ---------------------------------------------------------------------------
# read_anomalies_since — unit
# ---------------------------------------------------------------------------


def test_read_anomalies_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert read_anomalies_since(tmp_path / "nope") == []


def test_read_anomalies_returns_events_in_timestamp_order(anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))
        logger.log(_event("2026-05-12T23:59:00+00:00", score=0.5))  # prior day file

    events = read_anomalies_since(anomalies_dir)
    timestamps = [e["timestamp"] for e in events]
    assert timestamps == sorted(timestamps)


def test_read_anomalies_filters_by_since(anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))
        logger.log(_event("2026-05-13T12:02:00+00:00", score=3.0))

    after = read_anomalies_since(anomalies_dir, since="2026-05-13T12:00:30+00:00")
    assert [e["score"] for e in after] == [2.0, 3.0]


def test_read_anomalies_raises_on_bad_since(anomalies_dir: Path) -> None:
    anomalies_dir.mkdir()
    with pytest.raises(ValueError):
        read_anomalies_since(anomalies_dir, since="not-a-date")


# ---------------------------------------------------------------------------
# read_latest_telemetry_rows — unit
# ---------------------------------------------------------------------------


def test_telemetry_latest_returns_empty_when_no_session(config: CanaryConfig) -> None:
    assert read_latest_telemetry_rows(config.collection.storage_path) == []


def test_telemetry_latest_returns_newest_session_newest_chunk(config: CanaryConfig) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=2)
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T130000", rows=3)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=10)
    assert len(rows) == 3
    assert all(r["source"] == "obd" for r in rows)
    assert all(r["channel"] == "RPM" for r in rows)
    # the helper collapses (value_number, value_text, value_bool) → "value"
    assert {"value", "unit", "timestamp"} <= set(rows[0])


def test_telemetry_latest_truncates_to_n(config: CanaryConfig) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=10)
    rows = read_latest_telemetry_rows(config.collection.storage_path, n=3)
    assert len(rows) == 3
    # the *last* 3 rows are returned (tail)
    assert rows[0]["value"] == 707.0


# ---------------------------------------------------------------------------
# echo_config
# ---------------------------------------------------------------------------


def test_echo_config_round_trips(config: CanaryConfig) -> None:
    payload = echo_config(config)
    assert payload["vehicle"]["model"] == "Xterra"
    assert payload["collection"]["obd_poll_rate_hz"] == 1
    # JSON-safe — no Path objects
    json.dumps(payload)


# ---------------------------------------------------------------------------
# FastAPI integration — full route surface
# ---------------------------------------------------------------------------


def _client(config: CanaryConfig, anomalies_dir: Path, models_dir: Path | None = None):
    app = create_app(config, anomalies_dir=anomalies_dir, models_dir=models_dir)
    return TestClient(app)


def test_status_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000")
    response = _client(config, anomalies_dir).get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["latest_session"] == "2026-05-13T120000"
    assert "Nissan Xterra" in body["vehicle"]


def test_config_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/config")
    assert response.status_code == 200
    assert response.json()["vehicle"]["model"] == "Xterra"


def test_anomalies_endpoint_empty(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/anomalies")
    assert response.status_code == 200
    assert response.json() == {"events": [], "count": 0}


def test_anomalies_endpoint_returns_events(config: CanaryConfig, anomalies_dir: Path) -> None:
    with AnomalyEventLogger(anomalies_dir) as logger:
        logger.log(_event("2026-05-13T12:00:00+00:00", score=1.0))
        logger.log(_event("2026-05-13T12:01:00+00:00", score=2.0))

    response = _client(config, anomalies_dir).get(
        "/anomalies", params={"since": "2026-05-13T12:00:30+00:00"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["events"][0]["score"] == 2.0


def test_anomalies_endpoint_400_on_bad_since(config: CanaryConfig, anomalies_dir: Path) -> None:
    anomalies_dir.mkdir()
    response = _client(config, anomalies_dir).get("/anomalies", params={"since": "garbage"})
    assert response.status_code == 400
    assert "bad `since`" in response.json()["detail"]


def test_telemetry_latest_endpoint(config: CanaryConfig, anomalies_dir: Path) -> None:
    _make_session_with_parquet(config.collection.storage_path, "2026-05-13T120000", rows=4)
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert all("value" in row for row in body["rows"])


def test_telemetry_latest_rejects_silly_n(config: CanaryConfig, anomalies_dir: Path) -> None:
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 0})
    assert response.status_code == 422  # FastAPI validation
    response = _client(config, anomalies_dir).get("/telemetry/latest", params={"n": 1_000_000})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# helper: build an event
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket streams (Task 5.4)
# ---------------------------------------------------------------------------


def test_ws_telemetry_pushes_published_messages(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, telemetry_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        # Wait until our subscriber registered, then publish
        # (the TestClient is synchronous; the subscribe happens during accept()).
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"timestamp": "now", "channel": "RPM", "value": 700})
        message = ws.receive_json()
        assert message == {"timestamp": "now", "channel": "RPM", "value": 700}


def test_ws_anomalies_pushes_published_events(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, anomalies_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/anomalies") as ws:
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"timestamp": "now", "source": "lstm_autoencoder", "score": 5.0})
        message = ws.receive_json()
        assert message["source"] == "lstm_autoencoder"


def test_ws_disconnect_removes_subscriber(config: CanaryConfig, anomalies_dir: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    bus = Broadcaster()
    app = create_app(config, anomalies_dir=anomalies_dir, telemetry_broadcaster=bus)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        assert bus.subscriber_count == 1
        ws.close()
    # subscriber count drops after the handler finishes its `finally` block
    import time

    for _ in range(50):
        if bus.subscriber_count == 0:
            break
        time.sleep(0.01)
    assert bus.subscriber_count == 0


def test_ws_endpoint_works_without_explicit_broadcaster(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    """No broadcaster passed → app creates a default one; ws still accepts."""
    app = create_app(config, anomalies_dir=anomalies_dir)
    client = TestClient(app)
    with client.websocket_connect("/ws/telemetry") as ws:
        # Publish via app.state's auto-created broadcaster
        bus = app.state.telemetry_broadcaster
        import time

        for _ in range(50):
            if bus.subscriber_count == 1:
                break
            time.sleep(0.01)
        bus.publish({"ping": 1})
        assert ws.receive_json() == {"ping": 1}


def _event(timestamp: str, *, score: float) -> AnomalyEvent:
    return AnomalyEvent(
        timestamp=timestamp,
        source="lstm_autoencoder",
        model_version="2026-05-13",
        score=score,
        threshold=1.0,
        affected_channels=("obd:RPM",),
        operating_mode="idle",
        session="2026-05-13T120000",
    )
