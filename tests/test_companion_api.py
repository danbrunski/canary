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
from tests.configs import write_isolated_config


def _write_config(tmp_path: Path, storage: Path) -> Path:
    return write_isolated_config(
        tmp_path,
        storage=storage,
        preprocessing={"rolling_window_seconds": 30},
        models={
            "isolation_forest": {"enabled": False},
            "lstm_autoencoder": {"sequence_length": 60, "latent_dim": 16},
        },
    )


def _config(tmp_path: Path) -> CanaryConfig:
    return CanaryConfig.from_yaml(_write_config(tmp_path, tmp_path / "pi-sessions"))


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


def test_local_session_export_checksum_matches_export_bytes(tmp_path: Path) -> None:
    import hashlib

    lib = CanaryLibrary(tmp_path / "library")
    lib.import_session(_make_telemetry_session(tmp_path / "remote"))
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)

    checksum = client.get("/control/sessions/2026-05-17T120000/export/checksum")
    assert checksum.status_code == 200
    body = checksum.json()
    assert body["session_id"] == "2026-05-17T120000"

    export = client.get("/control/sessions/2026-05-17T120000/export")
    assert export.status_code == 200
    assert hashlib.sha256(export.content).hexdigest() == body["sha256"]
    assert body["size_bytes"] == len(export.content)


def test_local_model_manifest_endpoint_round_trips_uploaded_manifest(tmp_path: Path) -> None:
    lib = CanaryLibrary(tmp_path / "library")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)

    missing = client.get("/control/models/isolation_forest/manifest")
    assert missing.status_code == 404

    upload = client.post(
        "/control/models/isolation_forest/artifact",
        json={
            "artifact_b64": base64.b64encode(b"model bytes").decode("ascii"),
            "manifest": {"model_version": "if_v1"},
        },
    )
    assert upload.status_code == 200

    found = client.get("/control/models/isolation_forest/manifest")
    assert found.status_code == 200
    assert found.json()["manifest"]["model_version"] == "if_v1"


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


def test_local_feedback_endpoints_support_offline_labeling(tmp_path: Path) -> None:
    """Task 6.35 — false-positive labeling works without the Pi: the same
    verdict flow the live dashboard's AnomalyPanel posts, answered by the
    companion's own feedback log."""
    lib = CanaryLibrary(tmp_path / "library")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)
    event = {
        "timestamp": "2026-05-17T12:00:02Z",
        "source": "isolation_forest",
        "model_version": "if_v1",
        "score": 1.0,
        "session": "2026-05-17T120000",
    }

    submitted = client.post(
        "/control/feedback",
        json={"event": event, "label": "false_positive", "reason": "known bump"},
    )
    assert submitted.status_code == 201
    assert submitted.json()["feedback"]["label"] == "false_positive"

    listed = client.get("/control/feedback").json()
    assert listed["count"] == 1
    assert listed["summary"]["false_positive"] == 1

    rejected = client.post("/control/feedback", json={"event": event, "label": "not-a-real-label"})
    assert rejected.status_code == 422


def test_local_session_chart_endpoint(tmp_path: Path) -> None:
    """Task 5.3: fixes companion's stale docstring "chart" promise
    (src/companion/library.py) — a real per-channel time-series read now
    exists over the local library's synced sessions."""
    lib = CanaryLibrary(tmp_path / "library")
    lib.import_session(_make_telemetry_session(tmp_path / "remote"), source_host="pi.local")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)

    response = client.get(
        "/control/sessions/2026-05-17T120000/chart", params={"channels": "obd:RPM"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["channels"]["obd:RPM"]["values"] == [700.0, 701.0, 702.0]


def test_local_session_gps_export_endpoints(tmp_path: Path) -> None:
    """Task 5.6a: GPX/KML export over a synced session in the local library."""
    lib = CanaryLibrary(tmp_path / "library")
    lib.import_session(_make_telemetry_session(tmp_path / "remote"), source_host="pi.local")
    app = companion_api.create_app(_config(tmp_path), library=lib, static_dir=None)
    client = TestClient(app)

    gpx = client.get("/control/sessions/2026-05-17T120000/gps.gpx")
    assert gpx.status_code == 200
    assert gpx.headers["content-type"].startswith("application/gpx+xml")

    kml = client.get("/control/sessions/2026-05-17T120000/gps.kml")
    assert kml.status_code == 200
    assert kml.headers["content-type"].startswith("application/vnd.google-earth.kml+xml")


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
        if path.endswith("/export/checksum"):
            return {"session_id": "2026-05-17T130000", "sha256": "expected-digest"}
        return {"ok": True}

    download_calls: list[tuple[str, str, str | None, Path, str | None]] = []

    def fake_download(
        host: str,
        session_id: str,
        *,
        token: str | None,
        dest_dir: Path,
        expected_sha256: str | None = None,
    ) -> bytes:
        download_calls.append((host, session_id, token, dest_dir, expected_sha256))
        return _session_zip(session_id)

    monkeypatch.setattr(companion_api, "_request_json", fake_json)
    monkeypatch.setattr(companion_api, "_download_session_export", fake_download)
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
    body = response.json()
    assert body["session"]["session_id"] == "2026-05-17T130000"
    assert body["sha256"] == "expected-digest"
    assert "action_id" in body
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
        "/control/sessions/2026-05-17T130000/export/checksum",
        "secret",
    )
    assert download_calls == [
        ("http://pi.local:8000", "2026-05-17T130000", "secret", lib.sync_dir, "expected-digest")
    ]
    # 6.34: the sync round-trip is recorded as a `done` queued action, so a
    # future interrupted sync would be visible/retryable via the same table.
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "sync_session"
    assert actions[-1]["status"] == "done"


def test_sync_session_from_pi_marks_action_failed_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_json(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"sha256": "does-not-matter"}

    def fake_download(*_a: Any, **_k: Any) -> bytes:
        raise RuntimeError("connection dropped mid-transfer")

    monkeypatch.setattr(companion_api, "_request_json", fake_json)
    monkeypatch.setattr(companion_api, "_download_session_export", fake_download)
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
        json={},
    )

    assert response.status_code == 502
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "sync_session"
    assert actions[-1]["status"] == "failed"
    assert "connection dropped" in actions[-1]["detail"]


def test_local_control_jobs_run_against_companion_controller(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, tmp_path / "sessions")
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


def _local_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_format_version": 1,
        "model_type": "isolation_forest",
        "model_version": "if_v1",
        "created_at": "2026-05-13T12:00:00+00:00",
        "training_data_hash": "sha256:deadbeef",
        "training_sessions": ["2026-05-13T120000"],
        "input_rows": 1000,
        "hyperparams": {"n_estimators": 100, "contamination": 0.01},
        "schema": {"feature_columns": ["obd:RPM", "obd:SPEED"]},
    }
    base.update(overrides)
    return base


def test_publish_model_to_pi_posts_existing_local_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        calls.append({"host": host, "method": method, "path": path, "token": token, "body": body})
        if path.endswith("/manifest"):
            return {"model_type": "isolation_forest", "manifest": _local_manifest()}
        if path == "/status":
            return {"health": {"active_model_version": "if_v1"}}
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
    manifest.write_text(json.dumps(_local_manifest()))
    client = TestClient(app)

    response = client.post("/companion/models/isolation_forest/publish", json={})

    assert response.status_code == 200
    publish = response.json()["publish"]
    assert publish["target"] == "http://canary.local:8000"
    # 6.37: accepted/active confirmation reads back the Pi's manifest +
    # /status active_model_version once the upload lands.
    assert publish["status"] == "active"
    assert publish["compatibility"]["compatible"] is True
    assert "action_id" in publish

    upload_call = next(c for c in calls if c["path"] == "/control/models/isolation_forest/artifact")
    assert upload_call["token"] == "pi-token"
    assert upload_call["body"]["artifact_b64"] == base64.b64encode(b"model bytes").decode("ascii")
    assert upload_call["body"]["manifest"] == _local_manifest()
    assert lib.publish_history("if_v1")[0]["status"] == "active"


def test_publish_model_to_pi_reports_schema_incompatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path.endswith("/manifest"):
            # Running model expects a narrower feature set than the
            # candidate - an expansion, so not hot-swappable.
            return {
                "model_type": "isolation_forest",
                "manifest": _local_manifest(schema={"feature_columns": ["obd:RPM"]}),
            }
        if path == "/status":
            return {"health": {"active_model_version": "if_v0"}}
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
    manifest.write_text(json.dumps(_local_manifest()))
    client = TestClient(app)

    response = client.post("/companion/models/isolation_forest/publish", json={})

    assert response.status_code == 200
    publish = response.json()["publish"]
    assert publish["compatibility"]["compatible"] is False
    assert publish["compatibility"]["hot_swappable"] is False
    # Upload still proceeds - the report is informational, not a gate.
    assert publish["status"] in {"accepted", "active", "uploaded"}


def test_publish_model_retryable_action_survives_a_failed_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single closure whose behavior flips after the first (failing) call -
    # the route's `_request_json` reference is captured once at `create_app`
    # time, so the test can't swap it out for a second monkeypatch call the
    # way other tests swap fakes *before* `create_app` runs.
    state = {"fail": True}

    def flaky_request(
        host: str,
        method: str,
        path: str,
        *,
        token: str | None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if path.endswith("/manifest"):
            return {"model_type": "isolation_forest", "manifest": None}
        if path == "/status":
            return {"health": {}}
        if state["fail"]:
            raise RuntimeError("POST /control/models/isolation_forest/artifact failed: offline")
        return {"model": {"artifact_bytes": 11}}

    monkeypatch.setattr(companion_api, "_request_json", flaky_request)
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
    manifest.write_text(json.dumps(_local_manifest()))
    client = TestClient(app)

    response = client.post("/companion/models/isolation_forest/publish", json={})

    assert response.status_code == 400
    actions = lib.list_actions()
    assert actions[-1]["kind"] == "publish_model"
    assert actions[-1]["status"] == "failed"

    # Retrying re-dispatches the same publish request against the same
    # (still-on-disk) artifact - this time the upload succeeds.
    state["fail"] = False
    retry = client.post(f"/companion/actions/{actions[-1]['id']}/retry")
    assert retry.status_code == 200
    assert retry.json()["publish"]["status"] == "uploaded"
    assert lib.list_actions()[-1]["status"] == "done"


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
