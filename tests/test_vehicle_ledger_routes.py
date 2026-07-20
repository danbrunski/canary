"""Tests for the `/control/vehicle-ledger` HTTP routes (Task 3.3)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.config import CanaryConfig
from src.dashboard.api import create_app

_CONFIG_YAML = """
vehicle: {{make: Nissan, model: Xterra, year: 2008}}
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
  isolation_forest: {{enabled: true, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder:
    enabled: false
    sequence_length: 30
    latent_dim: 8
    threshold_k: 3.0
dashboard: {{host: 127.0.0.1, port: 8000}}
"""


def _client(tmp_path: Path) -> tuple[TestClient, VehicleLedgerStore]:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(_CONFIG_YAML.format(storage=storage))
    config = CanaryConfig.from_yaml(cfg)
    store = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(
        config,
        anomalies_dir=anomalies,
        control_token="secret",
        vehicle_ledger_store=store,
    )
    return TestClient(app), store


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_post_vehicle_ledger_requires_auth(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        json={"occurred_at": "2026-07-19", "kind": "maintenance", "title": "Oil change"},
    )
    assert resp.status_code == 401


def test_post_and_get_vehicle_ledger(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={
            "occurred_at": "2026-07-19",
            "kind": "mod",
            "title": "Cold air intake",
            "affected_channels": ["obd:MAF"],
            "corpus_boundary": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()["event"]
    assert body["kind"] == "mod"
    assert body["corpus_boundary"] is True
    assert store.get(body["id"]) is not None

    got = client.get("/control/vehicle-ledger", headers=_auth())
    assert got.status_code == 200
    listed = got.json()
    assert listed["count"] == 1
    assert listed["events"][0]["title"] == "Cold air intake"


def test_invalid_kind_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/control/vehicle-ledger",
        headers=_auth(),
        json={"occurred_at": "2026-07-19", "kind": "bogus", "title": "?"},
    )
    assert resp.status_code == 422


def test_vehicle_ledger_503_when_not_configured(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(_CONFIG_YAML.format(storage=storage))
    config = CanaryConfig.from_yaml(cfg)
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(config, anomalies_dir=anomalies, control_token="secret")
    client = TestClient(app)
    resp = client.get("/control/vehicle-ledger", headers=_auth())
    assert resp.status_code == 503
