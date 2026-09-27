"""Tests for /control/replay HTTP endpoints + replay block in /status."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.alerts.event_logger import AnomalyEventLogger
from src.config import CanaryConfig
from src.control.replay_control import ReplayController
from src.dashboard.api import create_app
from src.storage.parquet_writer import ParquetTelemetryWriter, write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config


def _write_fresh_rpm(storage_path: Path, rpm: float) -> None:
    """Write one fresh OBD RPM sample to a live session dir that sorts after
    the ``2026-05-18T120000`` fixture session used below —
    ``_derive_vehicle_state`` reads the *newest* session's Parquet, same as
    ``GET /status``. Uses the batch writer (not ``ParquetTelemetryWriter``,
    which the ``replay_controller`` fixture already instantiated against the
    same storage root this test run — a second real-time writer collides on
    the same second-resolution session dir name)."""
    write_records_to_parquet(
        [TelemetryRecord.obd(time.time(), "RPM", rpm, "rpm")],
        storage_path / "2026-05-18T235959",
    )


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    return isolated_config(
        tmp_path,
        "daemon",
        storage=storage,
        models={"isolation_forest": {"enabled": True}, "lstm_autoencoder": {"enabled": True}},
        dashboard={"port": 8000},
    )


@pytest.fixture
def anomalies_dir(tmp_path: Path) -> Path:
    return tmp_path / "anomalies"


@pytest.fixture
def replay_controller(tmp_path: Path) -> ReplayController:
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    anomalies = tmp_path / "anomalies"
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    return ReplayController(
        queue=queue,
        writer=ParquetTelemetryWriter(storage),
        anomaly_logger=AnomalyEventLogger(anomalies),
        storage_root=storage,
        broadcast_stale_timeout_seconds=10.0,
    )


_AUTH = {"Authorization": "Bearer secret"}


def test_replay_endpoints_unavailable_without_controller(
    config: CanaryConfig, anomalies_dir: Path
) -> None:
    """The replay surface should 503 when no controller was wired in."""
    app = create_app(config, anomalies_dir=anomalies_dir, control_token="secret")
    client = TestClient(app)
    response = client.post(
        "/control/replay",
        json={"session_id": "2026-05-18T120000"},
        headers=_AUTH,
    )
    assert response.status_code == 503


def test_replay_start_404_for_missing_session(
    config: CanaryConfig, anomalies_dir: Path, replay_controller: ReplayController
) -> None:
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)
    response = client.post(
        "/control/replay",
        json={"session_id": "2026-01-01T000000"},
        headers=_AUTH,
    )
    assert response.status_code == 404


def test_replay_start_returns_201_with_status_body(
    config: CanaryConfig,
    anomalies_dir: Path,
    replay_controller: ReplayController,
    tmp_path: Path,
) -> None:
    """End-to-end wiring: HTTP → ReplayController → status body shape.

    ``TestClient`` spins up a fresh event loop per request, so the
    background replay task gets orphaned the moment ``post`` returns —
    runtime lifecycle (start/stop, conflict, missing-session) is covered
    by the controller-level tests in ``test_replay_control``. The job
    here is just to prove the route is mounted, auth is required, the
    schema deserializes, and the controller is reachable.
    """
    source = tmp_path / "storage" / "2026-05-18T120000"
    source.mkdir(parents=True, exist_ok=True)
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)

    start = client.post(
        "/control/replay",
        json={"session_id": "2026-05-18T120000", "speed": 2.0, "loop": False},
        headers=_AUTH,
    )
    assert start.status_code == 201
    body = start.json()["replay"]
    assert body["running"] is True
    assert body["source_session_id"] == "2026-05-18T120000"
    assert body["speed"] == 2.0


def test_replay_start_refused_409_when_engine_running(
    config: CanaryConfig,
    anomalies_dir: Path,
    replay_controller: ReplayController,
    tmp_path: Path,
) -> None:
    """7.2 (operator-approved decision): replay is a bench/desk tool — refuse
    outright with a reason string rather than starting alongside a running
    engine."""
    _write_fresh_rpm(config.collection.storage_path, 2500.0)
    source = tmp_path / "storage" / "2026-05-18T120000"
    source.mkdir(parents=True, exist_ok=True)
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)

    response = client.post(
        "/control/replay",
        json={"session_id": "2026-05-18T120000"},
        headers=_AUTH,
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "engine_running" in detail
    # The controller must never have actually started.
    assert client.get("/control/replay", headers=_AUTH).json()["replay"]["running"] is False


def test_replay_start_not_refused_when_ignition_only(
    config: CanaryConfig,
    anomalies_dir: Path,
    replay_controller: ReplayController,
    tmp_path: Path,
) -> None:
    """RPM == 0 with OBD fresh classifies as ``ignition_only``, not
    ``engine_running`` — the 7.2 refusal must not trip here."""
    _write_fresh_rpm(config.collection.storage_path, 0.0)
    source = tmp_path / "storage" / "2026-05-18T120000"
    source.mkdir(parents=True, exist_ok=True)
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)

    response = client.post(
        "/control/replay",
        json={"session_id": "2026-05-18T120000"},
        headers=_AUTH,
    )
    assert response.status_code == 201


def test_replay_stop_returns_idle_status(
    config: CanaryConfig, anomalies_dir: Path, replay_controller: ReplayController
) -> None:
    """Stopping a never-started replay is a 409 (idempotent guard)."""
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)
    again = client.post("/control/replay/stop", headers=_AUTH)
    assert again.status_code == 409


def test_replay_get_returns_idle_status_when_not_running(
    config: CanaryConfig, anomalies_dir: Path, replay_controller: ReplayController
) -> None:
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)
    status = client.get("/control/replay", headers=_AUTH)
    assert status.status_code == 200
    assert status.json()["replay"]["running"] is False


def test_status_includes_replay_block_when_controller_wired(
    config: CanaryConfig, anomalies_dir: Path, replay_controller: ReplayController
) -> None:
    app = create_app(
        config,
        anomalies_dir=anomalies_dir,
        replay_controller=replay_controller,
        control_token="secret",
    )
    client = TestClient(app)
    body = client.get("/status").json()
    assert "replay" in body
    assert body["replay"]["running"] is False
