"""Tests for the ``replay_window`` job kind wiring on the daemon side."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.control.jobs import ALLOWED_JOB_KINDS, JobController


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


def _controller(tmp_path: Path) -> JobController:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(storage))
    config = CanaryConfig.from_yaml(cfg_path)
    return JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )


def test_replay_window_is_allow_listed() -> None:
    assert "replay_window" in ALLOWED_JOB_KINDS


def test_replay_window_command_includes_session_and_out(tmp_path: Path) -> None:
    """The command names tools/replay_window.py with --session-id and --out."""
    controller = _controller(tmp_path)
    cmd = controller._build_command(  # noqa: SLF001 - command shape is the contract
        "replay_window",
        {"session_id": "2026-05-17T120000"},
        job_id="job-42",
    )
    assert "tools/replay_window.py" in cmd
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "2026-05-17T120000"
    assert "--out" in cmd
    out_arg = cmd[cmd.index("--out") + 1]
    assert out_arg.endswith("job-42.replay.json")


def test_replay_window_command_forwards_optional_params(tmp_path: Path) -> None:
    """``start``/``end``/``if_model``/``lstm_model``/``anomalies_dir`` flow through."""
    controller = _controller(tmp_path)
    cmd = controller._build_command(  # noqa: SLF001
        "replay_window",
        {
            "session_id": "S1",
            "start": "2026-05-17T12:00:00Z",
            "end": "2026-05-17T12:10:00Z",
            "if_model": "/tmp/if.joblib",
            "lstm_model": "/tmp/lstm",
            "anomalies_dir": "/var/canary/anomalies",
            "max_rows": 1000,
            "max_windows": 500,
        },
        job_id="job-1",
    )
    assert "--start" in cmd
    assert "--end" in cmd
    assert "--if-model" in cmd
    assert "--lstm-model" in cmd
    assert "--anomalies-dir" in cmd
    assert "--max-rows" in cmd
    assert cmd[cmd.index("--max-rows") + 1] == "1000"
    assert cmd[cmd.index("--max-windows") + 1] == "500"


def test_replay_window_requires_session_id(tmp_path: Path) -> None:
    """The command builder rejects a missing session_id up front."""
    controller = _controller(tmp_path)
    with pytest.raises(ValueError, match="session_id"):
        controller._build_command("replay_window", {}, job_id="x")  # noqa: SLF001
