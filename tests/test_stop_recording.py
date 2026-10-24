"""Tests for the pidfile-based stop helper's pure decision logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.stop_recording import (
    _looks_like_recorder,
    plan_stop,
    read_recorder_pid,
    resolve_session,
    summarize_manifest,
)
from src.session import MANIFEST_FILENAME, PID_FILENAME


@pytest.mark.parametrize(
    ("pid", "alive", "manifest", "expected"),
    [
        (None, False, False, "no-target"),
        (None, False, True, "already-stopped"),
        (1234, False, False, "stale-no-manifest"),
        (1234, False, True, "already-stopped"),
        (1234, True, False, "signal"),
        (1234, True, True, "signal"),  # alive wins even if a manifest exists
    ],
)
def test_plan_stop(pid: int | None, alive: bool, manifest: bool, expected: str) -> None:
    assert plan_stop(pid, alive, manifest) == expected


def test_read_recorder_pid_valid(tmp_path: Path) -> None:
    (tmp_path / PID_FILENAME).write_text("4242\n")
    assert read_recorder_pid(tmp_path) == 4242


def test_read_recorder_pid_absent(tmp_path: Path) -> None:
    assert read_recorder_pid(tmp_path) is None


def test_read_recorder_pid_garbage(tmp_path: Path) -> None:
    (tmp_path / PID_FILENAME).write_text("not-a-pid")
    assert read_recorder_pid(tmp_path) is None


def test_summarize_manifest_valid(tmp_path: Path) -> None:
    manifest = tmp_path / MANIFEST_FILENAME
    manifest.write_text(
        json.dumps(
            {
                "frames_recorded": 1133939,
                "duration_seconds": 816.4,
                "parse_errors": 0,
                "reconnects": 0,
                "dropped_writes": 0,
                "stopped_by": "signal",
            }
        )
    )
    summary = summarize_manifest(manifest)
    assert summary is not None
    assert "frames=1133939" in summary
    assert "parse_errors=0" in summary
    assert "stopped_by='signal'" in summary


def test_summarize_manifest_absent(tmp_path: Path) -> None:
    assert summarize_manifest(tmp_path / MANIFEST_FILENAME) is None


def test_summarize_manifest_invalid_json(tmp_path: Path) -> None:
    manifest = tmp_path / MANIFEST_FILENAME
    manifest.write_text("{ truncated")
    assert summarize_manifest(manifest) is None


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        (".venv/bin/python scripts/record.py --config canary.yaml", True),
        ("/usr/bin/python3 scripts/record.py", True),
        ("nginx: worker process", False),
        ("python scripts/stop_recording.py", False),
    ],
)
def test_looks_like_recorder(cmdline: str, expected: bool) -> None:
    assert _looks_like_recorder(cmdline) is expected


def test_resolve_session_explicit(tmp_path: Path) -> None:
    sess = tmp_path / "2026-05-16T202800"
    sess.mkdir()
    assert resolve_session(sess, tmp_path) == sess


def test_resolve_session_explicit_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_session(tmp_path / "nope", tmp_path)


def test_resolve_session_latest(tmp_path: Path) -> None:
    (tmp_path / "2026-05-16T120000").mkdir()
    newest = tmp_path / "2026-05-16T202800"
    newest.mkdir()
    assert resolve_session(None, tmp_path) == newest


def test_resolve_session_latest_none(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_session(None, tmp_path)
