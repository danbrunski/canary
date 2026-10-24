"""Tests for the systemd service-health read tool."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest

from src.mcp_server.tools import service_health as health_tool
from tests.fakes import completed_process


def _show_output(
    active_state: str = "active",
    sub_state: str = "running",
    restarts: int = 0,
    monotonic_usec: int = 1_000_000,
) -> str:
    return (
        f"ActiveState={active_state}\n"
        f"SubState={sub_state}\n"
        f"NRestarts={restarts}\n"
        f"ActiveEnterTimestampMonotonic={monotonic_usec}\n"
    )


def test_unavailable_when_systemctl_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    out = health_tool.get_service_health()
    assert out == {"available": False, "services": []}


def test_reports_healthy_service_with_uptime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(health_tool, "_read_uptime_seconds", lambda: 3600.0)

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "systemctl"
        assert cmd[1] == "show"
        return completed_process(
            stdout=_show_output(monotonic_usec=1_000_000)
        )  # active for 1s (monotonic)

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = health_tool.get_service_health(["canary"])
    assert out["available"] is True
    svc = out["services"][0]
    assert svc["unit"] == "canary"
    assert svc["is_active"] is True
    assert svc["active_state"] == "active"
    assert svc["sub_state"] == "running"
    assert svc["restart_count"] == 0
    assert svc["uptime_seconds"] == pytest.approx(3599.0)  # 3600s boot - 1s since-active


def test_flags_crash_loop_via_restart_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A climbing NRestarts is the crash-loop tell (what the UPS watchdog did
    before its power-loss-detect fix)."""
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(health_tool, "_read_uptime_seconds", lambda: 100.0)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: completed_process(stdout=_show_output(restarts=47)),
    )
    out = health_tool.get_service_health(["ups-shutdown"])
    assert out["services"][0]["restart_count"] == 47


def test_inactive_service_has_no_uptime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(health_tool, "_read_uptime_seconds", lambda: 100.0)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: completed_process(
            stdout=_show_output(active_state="inactive", sub_state="dead")
        ),
    )
    out = health_tool.get_service_health(["canary-mcp"])
    svc = out["services"][0]
    assert svc["is_active"] is False
    assert svc["uptime_seconds"] is None


def test_nonzero_exit_surfaces_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **_: completed_process(stderr="unit not found", returncode=1),
    )
    out = health_tool.get_service_health(["nonexistent"])
    assert out["services"][0]["error"] == "unit not found"


def test_defaults_to_the_three_canary_units(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(health_tool, "_read_uptime_seconds", lambda: None)
    captured: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(cmd[2])  # unit name is argv[2]
        return completed_process(stdout=_show_output())

    monkeypatch.setattr(subprocess, "run", fake_run)
    health_tool.get_service_health()
    assert captured == ["canary", "canary-mcp", "ups-shutdown"]


def test_subprocess_error_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/systemctl")

    def raise_oserror(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("boom")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    out = health_tool.get_service_health(["canary"])
    assert "boom" in out["services"][0]["error"]
