"""Tests for the authenticated daemon control API."""

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

from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.dashboard.api import create_app
from src.dashboard.broadcast import Broadcaster


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
daemon:
  catalog_db: {storage_path.parent / "catalog.db"}
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


def test_session_metadata_pin_unpin_and_delete(tmp_path: Path) -> None:
    client, session = _client(tmp_path)

    metadata = client.post(
        f"/control/sessions/{session.name}/metadata",
        headers=_auth(),
        json={"metadata": {"route": "mesa"}},
    )
    assert metadata.status_code == 200
    assert metadata.json()["metadata"]["route"] == "mesa"

    pinned = client.post(
        f"/control/sessions/{session.name}/pin",
        headers=_auth(),
        json={"reason": "good drive", "metadata": {"terrain": "city"}},
    )
    assert pinned.status_code == 200
    assert pinned.json()["session"]["pinned"] is True
    assert pinned.json()["session"]["metadata"]["terrain"] == "city"

    blocked = client.delete(f"/control/sessions/{session.name}", headers=_auth())
    assert blocked.status_code == 409

    unpinned = client.post(f"/control/sessions/{session.name}/unpin", headers=_auth())
    assert unpinned.status_code == 200
    assert unpinned.json()["session"]["pinned"] is False

    deleted = client.delete(f"/control/sessions/{session.name}", headers=_auth())
    assert deleted.status_code == 200
    assert not session.exists()


def test_audit_log_and_control_websocket_receive_events(tmp_path: Path) -> None:
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
        control_broadcaster=Broadcaster(),
        audit_logger=ControlAuditLogger(tmp_path / "audit.jsonl"),
        control_token="secret",
    )
    client = TestClient(app)

    with client.websocket_connect("/ws/control?token=secret") as ws:
        response = client.post("/control/captures", headers=_auth(), json={"purpose": "drive"})
        assert response.status_code == 201
        event = ws.receive_json()

    assert event["action"] == "capture.start"
    audit = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert audit["action"] == "capture.start"
    assert audit["payload"]["purpose"] == "drive"


def test_control_preflight_and_version(tmp_path: Path) -> None:
    client, _session = _client(tmp_path)
    started = client.post("/control/captures", headers=_auth(), json={})
    assert started.status_code == 201

    preflight = client.get("/control/preflight", headers=_auth())
    assert preflight.status_code == 200
    payload = preflight.json()
    assert payload["ok"] is True
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["control_token"]["ok"] is True
    assert checks["active_session_writable"]["ok"] is True
    assert checks["can_interface"]["ok"] is True

    version = client.get("/control/version", headers=_auth())
    assert version.status_code == 200
    assert version.json()["api_version"] == 1
    assert version.json()["config_token_env"] == "CANARY_CONTROL_TOKEN"


def test_control_model_artifact_upload_publishes_watched_files(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    config = config.model_copy(
        update={"daemon": config.daemon.model_copy(update={"models_dir": tmp_path / "models"})}
    )
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        capture_controller=CaptureController(lambda: session),
        control_token="secret",
    )
    client = TestClient(app)

    response = client.post(
        "/control/models/isolation_forest/artifact",
        headers=_auth(),
        json={
            "artifact_b64": base64.b64encode(b"model bytes").decode("ascii"),
            "manifest": {"version": "test-model"},
        },
    )

    assert response.status_code == 200
    model = response.json()["model"]
    artifact_path = Path(model["artifact_path"])
    manifest_path = Path(model["manifest_path"])
    assert artifact_path.read_bytes() == b"model bytes"
    assert json.loads(manifest_path.read_text())["version"] == "test-model"


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

    ledger_controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
        command_builder=_command_builder,
    )
    assert ledger_controller.get(job_id) is not None


def test_retrain_job_command_passes_job_id_to_ledger(tmp_path: Path) -> None:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "storage"))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )

    command = controller._build_command(  # noqa: SLF001 - command shape is the contract here
        "retrain_if",
        {"all": True},
        job_id="job-123",
    )

    assert "--job-id" in command
    assert command[command.index("--job-id") + 1] == "job-123"


def test_retrain_job_command_defaults_compute_target_to_pi(tmp_path: Path) -> None:
    """Task 6.36: the Pi daemon's own JobController (no override) records
    'pi'; the companion's (`_make_job_controller`) passes 'laptop' instead
    (covered by `test_companion_services`/`test_companion_api`)."""
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "storage"))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )

    for kind in ("retrain_if", "retrain_lstm", "retrain_tcn", "retrain_all"):
        command = controller._build_command(kind, {}, job_id="job-1")  # noqa: SLF001
        assert "--compute-target" in command, kind
        assert command[command.index("--compute-target") + 1] == "pi", kind


def test_retrain_all_job_command_since_param_builds_since_flag(tmp_path: Path) -> None:
    """Task 3.1/3.2: the online-training scheduler's `since` param becomes
    `retrain.py --since <date>` — and there is no params-level path to
    `--no-calibration-holdout` anywhere in the command builder, so a
    scheduled retrain structurally cannot disable the calibration holdout."""
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "storage"))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )

    command = controller._build_command(  # noqa: SLF001 - command shape is the contract here
        "retrain_all",
        {"since": "2026-06-01"},
    )

    assert "--since" in command
    assert command[command.index("--since") + 1] == "2026-06-01"
    assert "--no-calibration-holdout" not in command
    assert "--days" not in command
    assert "--all" not in command


def test_export_dataset_job_command_shape(tmp_path: Path) -> None:
    """Task 5.6b: the Export panel's job kind builds the exact
    ``scripts/export_dataset.py`` invocation an operator would type by hand."""
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    (storage / "2026-05-18T090000").mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )

    command = controller._build_command(  # noqa: SLF001 - command shape is the contract here
        "export_dataset",
        {
            "session_ids": ["2026-05-17T120000", "2026-05-18T090000"],
            "channels": ["obd:RPM"],
            "resample_hz": 1.0,
            "formats": ["csv"],
            "purpose": "megalogviewer",
            "zip": False,
        },
        job_id="job-456",
    )

    assert "scripts/export_dataset.py" in command[1]
    assert command.count("--session") == 2
    assert "2026-05-17T120000" in command
    assert "2026-05-18T090000" in command
    assert command[command.index("--channel") + 1] == "obd:RPM"
    assert command[command.index("--resample-hz") + 1] == "1.0"
    assert command[command.index("--format") + 1] == "csv"
    assert command[command.index("--purpose") + 1] == "megalogviewer"
    assert "--no-zip" in command


def test_export_dataset_job_requires_session_ids(tmp_path: Path) -> None:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path / "storage"))
    config = CanaryConfig.from_yaml(cfg_path)
    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )

    with pytest.raises(ValueError, match="session_ids"):
        controller._build_command("export_dataset", {}, job_id="job-789")  # noqa: SLF001


def test_control_jobs_can_cancel_running_process(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)

    def _command_builder(_kind: str, _params: dict[str, Any]) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(10)"]

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
        json={"kind": "corpus_report", "params": {}},
    )
    job_id = created.json()["job"]["job_id"]

    cancelled = client.post(f"/control/jobs/{job_id}/cancel", headers=_auth())
    assert cancelled.status_code == 200

    final = None
    for _ in range(80):
        final = client.get(f"/control/jobs/{job_id}", headers=_auth()).json()["job"]
        if final["status"] == "cancelled":
            break
        time.sleep(0.05)
    assert final is not None
    assert final["status"] == "cancelled"


def test_control_jobs_mark_interrupted_ledger_jobs_failed(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    session = storage / "2026-05-17T120000"
    session.mkdir(parents=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "jobs.jsonl").write_text(
        json.dumps(
            {
                "job_id": "interrupted",
                "kind": "corpus_report",
                "status": "running",
                "created_at": "2026-05-17T12:00:00+00:00",
                "log_path": str(jobs_dir / "interrupted.log"),
                "command": [sys.executable, "-c", "print('old')"],
                "params": {},
            }
        )
        + "\n"
    )

    controller = JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=jobs_dir,
        python=Path(sys.executable),
    )

    job = controller.get("interrupted")
    assert job is not None
    assert job.status == "failed"
    assert job.finished_at is not None


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


def test_require_control_auth_constant_time_paths() -> None:
    """The bearer guard: 503 when unset, 401 on missing/malformed/wrong,
    pass on exact match — now via secrets.compare_digest (constant time)."""
    import pytest
    from fastapi import HTTPException

    from src.dashboard.api import _require_control_auth

    # Server token not configured → fail closed with 503, never 401.
    for unset in (None, ""):
        with pytest.raises(HTTPException) as exc:
            _require_control_auth(unset, "Bearer whatever")
        assert exc.value.status_code == 503

    # Missing / malformed / wrong → 401.
    for bad in (None, "", "token-only", "Basic abc", "Bearer", "Bearer wrong"):
        with pytest.raises(HTTPException) as exc:
            _require_control_auth("s3cret", bad)
        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    # Exact match → no raise.
    _require_control_auth("s3cret", "Bearer s3cret")


def test_loopback_open_when_token_unconfigured() -> None:
    """No token configured: control is open to a loopback client (dev/sim)
    but still 503s for anyone else. A configured token is always enforced,
    loopback or not (the Pi is unaffected)."""
    import pytest
    from fastapi import HTTPException

    from src.dashboard.api import _require_control_auth

    # Unconfigured + loopback → allowed (no raise).
    for unset in (None, ""):
        _require_control_auth(unset, None, client_is_loopback=True)

    # Unconfigured + non-loopback → still fail closed with 503.
    for unset in (None, ""):
        with pytest.raises(HTTPException) as exc:
            _require_control_auth(unset, None, client_is_loopback=False)
        assert exc.value.status_code == 503

    # Configured token enforced regardless of where the request came from.
    with pytest.raises(HTTPException) as exc:
        _require_control_auth("s3cret", "Bearer wrong", client_is_loopback=True)
    assert exc.value.status_code == 401
    _require_control_auth("s3cret", "Bearer s3cret", client_is_loopback=True)


def test_client_is_loopback_detects_socket_peer() -> None:
    from starlette.requests import Request

    from src.dashboard.api import _client_is_loopback

    def req(client: tuple[str, int] | None) -> Request:
        scope: dict[str, object] = {"type": "http", "headers": []}
        if client is not None:
            scope["client"] = client
        return Request(scope)

    assert _client_is_loopback(req(("127.0.0.1", 1))) is True
    assert _client_is_loopback(req(("127.9.9.9", 1))) is True  # whole 127/8
    assert _client_is_loopback(req(("::1", 1))) is True
    assert _client_is_loopback(req(("192.168.1.50", 1))) is False
    assert _client_is_loopback(req(("10.0.0.2", 1))) is False
    assert _client_is_loopback(req(("testclient", 1))) is False  # unparseable
    assert _client_is_loopback(req(None)) is False  # no client (e.g. raw ASGI)
