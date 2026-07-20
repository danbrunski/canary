"""Tests for the local companion API vertical slice."""

from __future__ import annotations

import base64
import json
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.feedback import FeedbackStore
from src.companion import api as companion_api
from src.companion.library import CanaryLibrary
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


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


def _make_telemetry_session(parent: Path, session_id: str = "2026-05-17T120000") -> Path:
    sdir = parent / session_id
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    records: list[TelemetryRecord] = []
    for index in range(3):
        ts = base + timedelta(seconds=index)
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="RPM", value=700 + index)
        )
        records.append(
            TelemetryRecord(timestamp=ts, source="obd", channel="SPEED", value=float(index))
        )
    write_records_to_parquet(records, sdir)
    (sdir / "manifest.json").write_text(json.dumps({"started_at": base.isoformat()}))
    return sdir


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


def test_local_dashboard_read_endpoints_use_library_files(tmp_path: Path) -> None:
    lib = CanaryLibrary(tmp_path / "library")
    lib.import_session(_make_telemetry_session(tmp_path / "remote"), source_host="pi.local")
    dataset_path = tmp_path / "dataset.parquet"
    dataset_path.write_bytes(b"dataset")
    lib.add_dataset(
        "baseline",
        path=dataset_path,
        manifest={"purpose": "baseline", "row_count": 6, "created_at": "2026-05-17T12:00:00Z"},
    )
    lib.queue_action("sync_session", {"session_id": "2026-05-17T120000"})
    lib.record_publish("if_v1", target="http://pi.local:8000", status="uploaded")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    event = {
        "timestamp": "2026-05-17T12:00:02Z",
        "source": "isolation_forest",
        "model_version": "if_v1",
        "score": 1.0,
        "session": "2026-05-17T120000",
    }
    with AnomalyEventLogger(app.state.config.daemon.anomalies_dir) as logger:
        logger.log(AnomalyEvent.model_validate(event))
    FeedbackStore(app.state.config.daemon.feedback_log).record(
        event=event,
        label="false_positive",
    )
    client = TestClient(app)

    telemetry = client.get("/telemetry/latest", params={"n": 2}).json()
    assert telemetry["count"] == 2
    assert telemetry["rows"][-1]["channel"] == "SPEED"

    anomalies = client.get("/anomalies").json()
    assert anomalies["count"] == 1
    assert anomalies["events"][0]["feedback"]["label"] == "false_positive"

    datasets = client.get("/control/datasets").json()
    assert datasets["count"] == 1
    assert datasets["datasets"][0]["manifest"]["purpose"] == "baseline"

    sync_state = client.get("/companion/sync/state").json()
    assert sync_state["source_hosts"] == ["pi.local"]
    assert len(sync_state["queued_actions"]) == 1
    assert sync_state["publish_history"][0]["status"] == "uploaded"


def test_local_session_metadata_and_pin_refresh_the_library_index(tmp_path: Path) -> None:
    """refactoring.md 1.2: companion's shared-handler callers must keep their
    library-index-refresh divergence from control's — control's equivalent
    routes (see `test_session_metadata_pin_unpin_and_delete` in
    test_control_api.py) never re-import and never return a "session" key
    from the metadata endpoint. If the shared `finalize`/`serialize` hooks
    were ever dropped in favor of control's plain summarize, this would
    start failing: no "session" key on the metadata response, and a stale
    (never advancing) `imported_at` on the pin/unpin responses."""
    lib = CanaryLibrary(tmp_path / "library")
    initial = lib.import_session(
        _make_telemetry_session(tmp_path / "remote"), source_host="pi.local"
    )
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)
    session_id = "2026-05-17T120000"

    metadata_response = client.post(
        f"/control/sessions/{session_id}/metadata",
        json={"metadata": {"route": "mesa"}},
    )
    assert metadata_response.status_code == 200
    body = metadata_response.json()
    # Control's own metadata endpoint never returns a "session" key at all
    # (test_control_api.test_session_metadata_pin_unpin_and_delete only
    # asserts on `metadata`) -- companion's does, because its handler passes
    # a `finalize` hook that reimports the session into the library index.
    assert body["metadata"]["route"] == "mesa"
    assert body["session"]["session_id"] == session_id
    # `imported_at` only exists on a `LibrarySession` re-import (never on
    # control's bare `SessionSummary.to_dict()`), and only *advances* if the
    # reimport actually ran rather than reusing the stale index entry.
    assert body["session"]["imported_at"] > initial.imported_at

    pin_response = client.post(
        f"/control/sessions/{session_id}/pin",
        json={"reason": "good drive", "metadata": {"terrain": "city"}},
    )
    assert pin_response.status_code == 200
    pin_body = pin_response.json()
    assert pin_body["metadata"]["terrain"] == "city"
    assert pin_body["session"]["pinned"] is True
    assert pin_body["session"]["imported_at"] > body["session"]["imported_at"]

    unpin_response = client.post(f"/control/sessions/{session_id}/unpin")
    assert unpin_response.status_code == 200
    unpin_body = unpin_response.json()
    assert unpin_body["session"]["pinned"] is False
    assert unpin_body["session"]["imported_at"] > pin_body["session"]["imported_at"]


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
