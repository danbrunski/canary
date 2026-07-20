"""Tests for `GET /control/sessions/{id}/chart` and the GPX/KML export
routes (Tasks 5.3/5.6a)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

_CONFIG_YAML = """
vehicle: {{make: Nissan, model: Xterra, year: 2008, vin: ""}}
hardware:
  elm327_port: /dev/ttyUSB0
  can: {{interface: virtual, channel: vcan0, bitrate: 500000}}
collection:
  obd_poll_rate_hz: 1.0
  storage_path: {storage}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1.0
  rolling_window_seconds: 5
  operating_modes: {{}}
models:
  isolation_forest: {{enabled: false, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder:
    enabled: false
    sequence_length: 30
    latent_dim: 8
    threshold_k: 3.0
dashboard: {{host: 127.0.0.1, port: 8000}}
"""

_SESSION_ID = "2026-05-17T120000"


def _client_and_session(tmp_path: Path) -> tuple[TestClient, Path]:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    session_dir = storage / _SESSION_ID
    records = []
    t0 = 1_780_000_000.0
    for i in range(30):
        t = t0 + i
        records.append(TelemetryRecord.obd(t, "RPM", 800.0 + i * 5, "rpm"))
        records.append(TelemetryRecord.gps(t, "latitude", 34.0 + i * 0.0001, "deg"))
        records.append(TelemetryRecord.gps(t, "longitude", -118.0 - i * 0.0001, "deg"))
        records.append(TelemetryRecord.gps(t, "altitude", 100.0 + i, "m"))
    write_records_to_parquet(records, session_dir, max_rows=10_000)

    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_CONFIG_YAML.format(storage=storage))
    config = CanaryConfig.from_yaml(cfg_path)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    return TestClient(app), session_dir


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_session_chart_requires_auth(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(f"/control/sessions/{_SESSION_ID}/chart", params={"channels": "obd:RPM"})
    assert response.status_code == 401


def test_session_chart_returns_raw_series(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(
        f"/control/sessions/{_SESSION_ID}/chart",
        params={"channels": "obd:RPM"},
        headers=_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resampled"] is False
    assert len(body["channels"]["obd:RPM"]["values"]) == 30
    assert body["channels"]["obd:RPM"]["values"][0] == 800.0


def test_session_chart_resampled(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(
        f"/control/sessions/{_SESSION_ID}/chart",
        params={"channels": "obd:RPM", "resample_hz": 1.0},
        headers=_auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resampled"] is True
    assert body["hz"] == 1.0


def test_session_chart_unknown_session_404s(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(
        "/control/sessions/2099-01-01T000000/chart",
        params={"channels": "obd:RPM"},
        headers=_auth(),
    )
    assert response.status_code == 404


def test_gps_gpx_export(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(f"/control/sessions/{_SESSION_ID}/gps.gpx", headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/gpx+xml")
    assert "<trkpt" in response.text
    assert response.text.count("<trkpt") == 30


def test_gps_kml_export(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(f"/control/sessions/{_SESSION_ID}/gps.kml", headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.google-earth.kml+xml")
    assert "<coordinates>" in response.text


def test_gps_export_requires_auth(tmp_path: Path) -> None:
    client, _session = _client_and_session(tmp_path)
    response = client.get(f"/control/sessions/{_SESSION_ID}/gps.gpx")
    assert response.status_code == 401
