"""Tests for the authenticated daemon control API."""

from __future__ import annotations

import json
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.dashboard.api import create_app


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
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 8000
"""


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = CaptureController(lambda: session)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        capture_controller=controller,
        control_token="secret",
    )
    return TestClient(app), session


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _read_tags(session: Path) -> list[dict[str, object]]:
    with (session / "tags.jsonl").open("r") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_control_routes_require_bearer_token(tmp_path: Path) -> None:
    client, _session = _client(tmp_path)

    response = client.post("/control/captures", json={})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_control_routes_require_configured_token(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        capture_controller=CaptureController(lambda: session),
        control_token="",
    )
    client = TestClient(app)

    response = client.post("/control/captures", headers=_auth(), json={})

    assert response.status_code == 503
    assert response.json()["detail"] == "control API token is not configured"


def test_start_tag_stop_capture_writes_session_tags(tmp_path: Path) -> None:
    client, session = _client(tmp_path)

    started = client.post(
        "/control/captures",
        headers=_auth(),
        json={
            "purpose": "next drive",
            "protocol": "drive",
            "vehicle_state": "driving",
            "fields": {"route": "mesa"},
        },
    )
    assert started.status_code == 201
    capture = started.json()["capture"]
    assert capture["session_id"] == session.name
    assert capture["purpose"] == "next drive"

    current = client.get("/control/captures/current", headers=_auth())
    assert current.status_code == 200
    assert current.json()["capture"]["capture_id"] == capture["capture_id"]

    tagged = client.post(
        "/control/captures/current/tags",
        headers=_auth(),
        json={"label": "highway_cruise", "fields": {"speed_target": 70}},
    )
    assert tagged.status_code == 200
    assert tagged.json()["tag"]["label"] == "highway_cruise"

    stopped = client.post(
        "/control/captures/current/stop",
        headers=_auth(),
        json={"reason": "arrived"},
    )
    assert stopped.status_code == 200
    assert stopped.json()["capture"]["capture_id"] == capture["capture_id"]

    after = client.get("/control/captures/current", headers=_auth())
    assert after.json()["capture"] is None

    tags = _read_tags(session)
    assert [tag["label"] for tag in tags] == [
        "capture_start",
        "highway_cruise",
        "capture_stop",
    ]
    assert tags[0]["capture_id"] == capture["capture_id"]
    assert tags[0]["protocol"] == "drive"
    assert tags[1]["speed_target"] == 70
    assert tags[2]["reason"] == "arrived"


def test_start_conflicts_when_capture_is_already_active(tmp_path: Path) -> None:
    client, _session = _client(tmp_path)

    first = client.post("/control/captures", headers=_auth(), json={})
    second = client.post("/control/captures", headers=_auth(), json={})

    assert first.status_code == 201
    assert second.status_code == 409
    assert "already active" in second.json()["detail"]


def test_tag_without_active_capture_conflicts(tmp_path: Path) -> None:
    client, _session = _client(tmp_path)

    response = client.post(
        "/control/captures/current/tags",
        headers=_auth(),
        json={"label": "orphan"},
    )

    assert response.status_code == 409
    assert "no active capture" in response.json()["detail"]


def test_session_list_detail_and_export(tmp_path: Path) -> None:
    client, session = _client(tmp_path)
    (session / "telemetry-00000.parquet").write_bytes(b"parquet-ish")
    (session / "tags.jsonl").write_text('{"label":"a"}\n{"label":"b"}\n')

    listed = client.get("/control/sessions", headers=_auth())
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["sessions"][0]["session_id"] == session.name
    assert listed.json()["sessions"][0]["tag_count"] == 2

    detail = client.get(f"/control/sessions/{session.name}", headers=_auth())
    assert detail.status_code == 200
    assert detail.json()["session"]["telemetry_chunks"] == 1

    exported = client.get(f"/control/sessions/{session.name}/export", headers=_auth())
    assert exported.status_code == 200
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        names = set(archive.namelist())
    assert f"{session.name}/telemetry-00000.parquet" in names
    assert f"{session.name}/tags.jsonl" in names


def test_control_jobs_run_restricted_background_commands(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)

    def _command_builder(_kind: str, _params: dict[str, Any]) -> list[str]:
        return [sys.executable, "-c", "print('job ok')"]

    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        capture_controller=CaptureController(lambda: session),
        job_controller=JobController(
            config=config,
            repo_root=tmp_path,
            config_path=cfg_path,
            jobs_dir=tmp_path / "jobs",
            python=Path(sys.executable),
            command_builder=_command_builder,
        ),
        control_token="secret",
    )
    client = TestClient(app)

    created = client.post(
        "/control/jobs",
        headers=_auth(),
        json={"kind": "corpus_report", "params": {"days": 7}},
    )
    assert created.status_code == 201
    job_id = created.json()["job"]["job_id"]

    final = None
    for _ in range(40):
        response = client.get(f"/control/jobs/{job_id}", headers=_auth())
        final = response.json()["job"]
        if final["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "succeeded"
    assert final["returncode"] == 0

    logs = client.get(f"/control/jobs/{job_id}/logs", headers=_auth())
    assert logs.status_code == 200
    assert "job ok" in logs.json()["log"]


def test_control_jobs_reject_unknown_kind(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        capture_controller=CaptureController(lambda: session),
        job_controller=JobController(
            config=config,
            repo_root=tmp_path,
            config_path=cfg_path,
            jobs_dir=tmp_path / "jobs",
            python=Path(sys.executable),
            command_builder=lambda _kind, _params: [sys.executable, "-c", "print('unused')"],
        ),
        control_token="secret",
    )
    client = TestClient(app)

    response = client.post(
        "/control/jobs",
        headers=_auth(),
        json={"kind": "shell", "params": {}},
    )

    assert response.status_code == 400
    assert "unsupported job kind" in response.json()["detail"]
