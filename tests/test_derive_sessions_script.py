"""``scripts/derive_sessions.py``: build derived frames up front (P01.2)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.derive_sessions import main

DRIVE_SLICE = Path("tests/fixtures/ground_truth/drive-slice")


def test_builds_then_reuses_then_reports_fresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "sessions"
    shutil.copytree(DRIVE_SLICE, root / "2026-08-03T231900")
    argv = [
        "--config",
        "canary.example.yaml",
        "--sessions-root",
        str(root),
        "--derived-dir",
        str(tmp_path / "derived"),
    ]

    assert main(argv) == 0
    assert "2026-08-03T231900: built" in capsys.readouterr().out
    assert main(argv) == 0
    assert "2026-08-03T231900: reused" in capsys.readouterr().out
    assert main([*argv, "--check"]) == 0
    assert capsys.readouterr().out.strip() == "2026-08-03T231900: fresh"


def test_no_sessions_is_an_error_not_an_empty_success(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    argv = ["--config", "canary.example.yaml", "--sessions-root", str(tmp_path / "empty")]
    assert main([*argv, "--derived-dir", str(tmp_path / "d")]) == 1
