"""Tests for the ``did_sweep`` job kind wiring on the daemon side."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.control.jobs import ALLOWED_JOB_KINDS, JobController
from tests.configs import write_isolated_config


def _controller(tmp_path: Path) -> JobController:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    cfg_path = write_isolated_config(tmp_path, "daemon", storage=storage)
    config = CanaryConfig.from_yaml(cfg_path)
    return JobController(
        config=config,
        repo_root=tmp_path,
        config_path=cfg_path,
        jobs_dir=tmp_path / "jobs",
        python=Path(sys.executable),
    )


def test_did_sweep_is_allow_listed() -> None:
    assert "did_sweep" in ALLOWED_JOB_KINDS


def test_did_sweep_command_includes_range_label_and_out(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    cmd = controller._build_command(  # noqa: SLF001 - command shape is the contract
        "did_sweep",
        {"start": "0x1100", "end": "0x12FF", "label": "control"},
        job_id="job-42",
    )
    assert "tools/did_sweep.py" in cmd
    assert cmd[cmd.index("--start") + 1] == "0x1100"
    assert cmd[cmd.index("--end") + 1] == "0x12FF"
    assert cmd[cmd.index("--label") + 1] == "control"
    out_arg = cmd[cmd.index("--out") + 1]
    assert out_arg.endswith("job-42.did_sweep.json")


def test_did_sweep_command_forwards_optional_params(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    cmd = controller._build_command(  # noqa: SLF001
        "did_sweep",
        {
            "start": 0x1100,
            "end": 0x12FF,
            "label": "wot",
            "header": "7E0",
            "session": "C0",
            "keepalive": 1.5,
            "delay_seconds": 0.05,
            "read_timeout": 0.5,
        },
        job_id="job-1",
    )
    assert cmd[cmd.index("--header") + 1] == "7E0"
    assert cmd[cmd.index("--session") + 1] == "C0"
    assert cmd[cmd.index("--keepalive") + 1] == "1.5"
    assert cmd[cmd.index("--delay-seconds") + 1] == "0.05"
    assert cmd[cmd.index("--read-timeout") + 1] == "0.5"


def test_did_sweep_requires_start_and_end(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    with pytest.raises(ValueError, match="start"):
        controller._build_command(  # noqa: SLF001
            "did_sweep", {"label": "control"}, job_id="x"
        )


def test_did_sweep_requires_label(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    with pytest.raises(ValueError, match="label"):
        controller._build_command(  # noqa: SLF001
            "did_sweep", {"start": 0, "end": 1}, job_id="x"
        )
