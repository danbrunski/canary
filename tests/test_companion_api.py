"""Tests for the local companion API vertical slice."""

from __future__ import annotations

import base64
import json
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.companion import api as companion_api
from src.companion.library import CanaryLibrary
from src.config import CanaryConfig
from src.control.jobs import JobController


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
  operating_modes: {{}}
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


def _config(tmp_path: Path) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "pi-sessions"))
    return CanaryConfig.from_yaml(cfg_path)


def _session_zip(session_id: str = "2026-05-17T120000") -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{session_id}/manifest.json", json.dumps({"started_at": "now"}))
        archive.writestr(f"{session_id}/tags.jsonl", '{"label":"baseline"}\n')
        archive.writestr(f"{session_id}/telemetry-00000.parquet", b"parquet-ish")
    return buf.getvalue()


def test_import_session_archive_endpoint_indexes_local_session(tmp_path: Path) -> None:
    lib = CanaryLibrary(tmp_path / "library")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)

    imported = client.post(
        "/companion/sessions/import",
        json={
            "archive_b64": base64.b64encode(_session_zip()).decode("ascii"),
            "source_host": "http://pi:8000",
            "index_with_config": False,
        },
    )

    assert imported.status_code == 201
    session = imported.json()["session"]
    assert session["session_id"] == "2026-05-17T120000"
    assert session["source_host"] == "http://pi:8000"
    assert (lib.sessions_dir / "2026-05-17T120000" / "manifest.json").exists()

    listed = client.get("/control/sessions")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["sessions"][0]["telemetry_chunks"] == 1


def test_sync_session_from_pi_uses_control_export_and_optional_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str, str | None, dict[str, Any] | None]] = []

    def fake_json(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append((host, method, path, token, body))
        return {"ok": True}

    def fake_bytes(host: str, method: str, path: str, *, token: str | None) -> bytes:
        calls.append((host, method, path, token, None))
        return _session_zip("2026-05-17T130000")

    monkeypatch.setattr(companion_api, "_request_json", fake_json)
    monkeypatch.setattr(companion_api, "_request_bytes", fake_bytes)
    lib = CanaryLibrary(tmp_path / "library")
    app = companion_api.create_app(
        _config(tmp_path),
        library=lib,
        pi_host="pi.local:8000",
        pi_token="secret",
        static_dir=None,
    )
    client = TestClient(app)

    response = client.post(
        "/companion/sync/sessions/2026-05-17T130000",
        json={"pin": True, "index_with_config": False},
    )

    assert response.status_code == 201
    assert response.json()["session"]["session_id"] == "2026-05-17T130000"
    assert calls[0] == (
        "http://pi.local:8000",
        "POST",
        "/control/sessions/2026-05-17T130000/pin",
        "secret",
        {"reason": "synced to companion library", "metadata": {}},
    )
    assert calls[1][:4] == (
        "http://pi.local:8000",
        "GET",
        "/control/sessions/2026-05-17T130000/export",
        "secret",
    )


def test_local_control_jobs_run_against_companion_controller(tmp_path: Path) -> None:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "sessions"))
    config = CanaryConfig.from_yaml(cfg_path)
    lib = CanaryLibrary(tmp_path / "library")

    def command_builder(_kind: str, _params: dict[str, Any]) -> list[str]:
        return [sys.executable, "-c", "print('local job ok')"]

    controller = JobController(
        config=companion_api.companion_config(config, lib),
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=lib.jobs_dir,
        python=Path(sys.executable),
        command_builder=command_builder,
    )
    app = companion_api.create_app(
        config,
        library=lib,
        job_controller=controller,
        static_dir=None,
    )
    client = TestClient(app)

    created = client.post("/control/jobs", json={"kind": "retrain_if", "params": {"days": 7}})
    assert created.status_code == 201
    job_id = created.json()["job"]["job_id"]

    final = None
    for _ in range(40):
        final = client.get(f"/control/jobs/{job_id}").json()["job"]
        if final["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "succeeded"

    logs = client.get(f"/control/jobs/{job_id}/logs")
    assert logs.status_code == 200
    assert "local job ok" in logs.json()["log"]


def test_publish_model_to_pi_posts_existing_local_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured.update(host=host, method=method, path=path, token=token, body=body)
        return {"model": {"artifact_bytes": 11}}

    monkeypatch.setattr(companion_api, "_request_json", fake_request)
    lib = CanaryLibrary(tmp_path / "library")
    app = companion_api.create_app(
        _config(tmp_path),
        library=lib,
        pi_host="http://canary.local:8000",
        pi_token="pi-token",
        static_dir=None,
    )
    local_config = app.state.config
    artifact = local_config.daemon.models_dir / "isolation_forest" / "model.joblib"
    manifest = artifact.with_suffix(".manifest.json")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"model bytes")
    manifest.write_text(json.dumps({"model_version": "if_v1"}))
    client = TestClient(app)

    response = client.post("/companion/models/isolation_forest/publish", json={})

    assert response.status_code == 200
    assert captured["host"] == "http://canary.local:8000"
    assert captured["method"] == "POST"
    assert captured["path"] == "/control/models/isolation_forest/artifact"
    assert captured["token"] == "pi-token"
    assert captured["body"]["artifact_b64"] == base64.b64encode(b"model bytes").decode("ascii")
    assert captured["body"]["manifest"] == {"model_version": "if_v1"}
    assert lib.publish_history("if_v1")[0]["status"] == "uploaded"


def test_companion_auth_is_optional_but_enforced_when_configured(tmp_path: Path) -> None:
    app = companion_api.create_app(
        _config(tmp_path),
        library=CanaryLibrary(tmp_path / "library"),
        control_token="local-secret",
        static_dir=None,
    )
    client = TestClient(app)

    assert client.get("/control/sessions").status_code == 401
    unlocked = client.get(
        "/control/sessions",
        headers={"Authorization": "Bearer local-secret"},
    )
    assert unlocked.status_code == 200
