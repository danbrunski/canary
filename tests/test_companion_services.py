"""Tests for the shared `/control/*` handlers + HTTP client in companion/services.py.

refactoring.md 1.2: these functions are the seam companion/routes.py and
dashboard/routes/control.py both call into. Covers the two things a naive
extraction could get wrong: (1) the urllib -> DaemonClient swap losing error
semantics, and (2) the optional hooks (`finalize`/`serialize`/`on_run`/etc.)
that carry each caller's one divergence not actually being called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.companion import services
from src.companion.library import CanaryLibrary


class _FakeDaemonClient:
    """Stand-in for `DaemonClient` capturing calls made against it."""

    instances: list[_FakeDaemonClient] = []

    def __init__(self, host: str, token: str | None, *, timeout: float) -> None:
        self.host = host
        self.token = token
        self.timeout = timeout
        self.calls: list[tuple[str, str, Any]] = []
        _FakeDaemonClient.instances.append(self)

    async def __aenter__(self) -> _FakeDaemonClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append(("GET", path, None))
        return {"ok": True, "path": path}

    async def post_json(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        self.calls.append(("POST", path, body))
        return {"ok": True, "body": body}

    async def get_bytes(self, path: str) -> bytes:
        self.calls.append(("GET", path, None))
        return b"zip-bytes"

    async def download_file(self, path: str, dest: Path, **_kwargs: Any) -> Path:
        self.calls.append(("GET", path, None))
        dest.write_bytes(b"zip-bytes")
        return dest


class _FailingDaemonClient(_FakeDaemonClient):
    async def get_json(self, path: str) -> dict[str, Any]:
        raise services.DaemonError("boom", status=404, detail="not found")

    async def get_bytes(self, path: str) -> bytes:
        raise services.DaemonError("boom", status=500, detail="server error")

    async def download_file(self, path: str, dest: Path, **_kwargs: Any) -> Path:
        raise services.DaemonError("boom", status=500, detail="server error")


@pytest.fixture(autouse=True)
def _reset_instances() -> None:
    _FakeDaemonClient.instances.clear()
    yield
    _FakeDaemonClient.instances.clear()


def test_request_json_delegates_to_daemon_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)

    result = services._request_json(
        "http://pi.local:8000", "POST", "/control/sessions/x/pin", token="secret", body={"a": 1}
    )

    assert result == {"ok": True, "body": {"a": 1}}
    client = _FakeDaemonClient.instances[0]
    assert client.host == "http://pi.local:8000"
    assert client.token == "secret"
    assert client.calls == [("POST", "/control/sessions/x/pin", {"a": 1})]


def test_request_bytes_delegates_to_daemon_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)

    result = services._request_bytes(
        "http://pi.local:8000", "GET", "/control/sessions/x/export", token=None
    )

    assert result == b"zip-bytes"


def test_request_json_wraps_daemon_error_as_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FailingDaemonClient)

    with pytest.raises(RuntimeError, match="failed"):
        services._request_json("http://pi.local:8000", "GET", "/control/jobs", token=None)


def test_request_bytes_wraps_daemon_error_as_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FailingDaemonClient)

    with pytest.raises(RuntimeError, match="failed"):
        services._request_bytes(
            "http://pi.local:8000", "GET", "/control/sessions/x/export", token=None
        )


def test_make_job_controller_records_laptop_compute_target(tmp_path: Path) -> None:
    """Task 6.36: the companion's controller (unlike the Pi daemon's,
    default 'pi') tags every retrain.py invocation 'laptop'."""
    from src.config import CanaryConfig

    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(
        f"""
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
  storage_path: {tmp_path / "sessions"}
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
    )
    config = CanaryConfig.from_yaml(cfg_path)
    controller = services._make_job_controller(
        config, repo_root=tmp_path, config_path=cfg_path, python=None
    )

    command = controller._build_command("retrain_if", {}, job_id="job-1")  # noqa: SLF001
    assert "--compute-target" in command
    assert command[command.index("--compute-target") + 1] == "laptop"


def test_download_session_export_delegates_to_daemon_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)

    data = services._download_session_export(
        "http://pi.local:8000", "2026-05-17T120000", token="secret", dest_dir=tmp_path
    )

    assert data == b"zip-bytes"
    # The downloaded file is cleaned up after being read into memory.
    assert not (tmp_path / "2026-05-17T120000.zip").exists()


def test_download_session_export_verifies_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FakeDaemonClient)
    import hashlib

    good = hashlib.sha256(b"zip-bytes").hexdigest()
    data = services._download_session_export(
        "http://pi.local:8000",
        "2026-05-17T120000",
        token=None,
        dest_dir=tmp_path,
        expected_sha256=good,
    )
    assert data == b"zip-bytes"

    with pytest.raises(ValueError, match="checksum mismatch"):
        services._download_session_export(
            "http://pi.local:8000",
            "2026-05-17T120000",
            token=None,
            dest_dir=tmp_path,
            expected_sha256="not-the-right-digest",
        )


def test_download_session_export_wraps_daemon_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "DaemonClient", _FailingDaemonClient)

    with pytest.raises(RuntimeError, match="failed"):
        services._download_session_export(
            "http://pi.local:8000", "2026-05-17T120000", token=None, dest_dir=tmp_path
        )


def test_run_queued_action_marks_done_on_success(tmp_path: Path) -> None:
    lib = CanaryLibrary(tmp_path / "library")
    result = services.run_queued_action(
        lib, "sync_session", {"session_id": "x"}, lambda: {"session": "x"}
    )
    assert result["session"] == "x"
    action = lib.list_actions()[0]
    assert action["kind"] == "sync_session"
    assert action["status"] == "done"
    assert result["action_id"] == action["id"]


def test_run_queued_action_marks_failed_and_reraises(tmp_path: Path) -> None:
    lib = CanaryLibrary(tmp_path / "library")

    def boom() -> dict[str, Any]:
        raise RuntimeError("network dropped")

    with pytest.raises(RuntimeError, match="network dropped"):
        services.run_queued_action(lib, "publish_model", {"model_type": "isolation_forest"}, boom)

    action = lib.list_actions()[0]
    assert action["status"] == "failed"
    assert "network dropped" in action["detail"]


def test_get_model_manifest_response_404_when_missing(tmp_path: Path) -> None:
    from fastapi import HTTPException

    missing = tmp_path / "model.manifest.json"
    with pytest.raises(HTTPException) as excinfo:
        services.get_model_manifest_response(
            "isolation_forest", resolve_manifest_path=lambda _mt: missing
        )
    assert excinfo.value.status_code == 404


def test_get_model_manifest_response_reads_json(tmp_path: Path) -> None:
    manifest_path = tmp_path / "model.manifest.json"
    manifest_path.write_text(json.dumps({"model_version": "if_v1"}))

    result = services.get_model_manifest_response(
        "isolation_forest", resolve_manifest_path=lambda _mt: manifest_path
    )
    assert result["manifest"]["model_version"] == "if_v1"


def test_training_runs_response_on_run_hook_is_optional(tmp_path: Path) -> None:
    ledger_path = tmp_path / "training_runs.jsonl"

    no_hook = services.training_runs_response(ledger_path)
    assert no_hook == {"runs": [], "count": 0}

    recorded: list[dict[str, Any]] = []
    with_hook = services.training_runs_response(ledger_path, on_run=recorded.append)
    assert with_hook == {"runs": [], "count": 0}
    assert recorded == []
