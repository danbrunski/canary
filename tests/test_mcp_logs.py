"""Tests for the daemon-log tail tool."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.mcp_server.tools import logs as logs_tool
from tests.fakes import completed_process


def test_tail_uses_journal_when_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When ``journalctl`` exists, it's invoked and its JSON lines parsed."""
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: "/usr/bin/journalctl")
    journal_lines = "\n".join(
        json.dumps({"MESSAGE": f"line {i}", "_SYSTEMD_UNIT": "canary.service"}) for i in range(3)
    )

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "journalctl"
        assert "--unit" in cmd
        return completed_process(stdout=journal_lines)

    monkeypatch.setattr(logs_tool.subprocess, "run", fake_run)
    out = logs_tool.tail_daemon_log(n=10)
    assert out["source"] == "journal"
    assert out["count"] == 3
    assert out["records"][0]["MESSAGE"] == "line 0"


def test_tail_reports_journal_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``journalctl`` exit surfaces in a single error record."""
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: "/usr/bin/journalctl")
    monkeypatch.setattr(
        logs_tool.subprocess,
        "run",
        lambda cmd, **_: completed_process(stderr="no such unit", returncode=1),
    )
    out = logs_tool.tail_daemon_log(n=5)
    assert out["source"] == "journal"
    assert out["records"][0]["error"] == "no such unit"


def test_tail_falls_back_to_file_when_journal_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ``journalctl``, the fallback file is read and parsed line-by-line."""
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: None)
    log = tmp_path / "canary.log"
    log.write_text(
        json.dumps({"timestamp": "t1", "message": "first"})
        + "\nnot-json-line\n"
        + json.dumps({"timestamp": "t2", "message": "third"})
        + "\n"
    )
    out = logs_tool.tail_daemon_log(n=10, fallback_path=log)
    assert out["source"].startswith("file:")
    assert out["count"] == 3
    assert out["records"][0]["message"] == "first"
    assert out["records"][1]["raw"] == "not-json-line"


def test_tail_is_unavailable_when_no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no journal and no fallback, the tool reports unavailable cleanly."""
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: None)
    out = logs_tool.tail_daemon_log(n=5)
    assert out["source"] == "unavailable"
    assert out["count"] == 0


def test_tail_clamps_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """``n`` is clamped to [1, 2000] so an LLM can't ask for a million."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: "/usr/bin/journalctl")

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return completed_process(stdout="")

    monkeypatch.setattr(logs_tool.subprocess, "run", fake_run)
    logs_tool.tail_daemon_log(n=10_000)
    assert "2000" in captured["cmd"]
    logs_tool.tail_daemon_log(n=0)
    assert "1" in captured["cmd"]


def test_tail_can_use_subprocess_timeoutexpired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5 s timeout is set so a hung journal never blocks the MCP loop."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(logs_tool.shutil, "which", lambda _: "/usr/bin/journalctl")

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        return completed_process(stdout="")

    monkeypatch.setattr(logs_tool.subprocess, "run", fake_run)
    logs_tool.tail_daemon_log(n=5)
    assert captured["timeout"] == 5


def test_real_subprocess_module_imported() -> None:
    """Sanity check that the module references the real ``subprocess`` (not a mock)."""
    assert logs_tool.subprocess is subprocess
