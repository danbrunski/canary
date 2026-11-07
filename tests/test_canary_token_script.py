"""``scripts/canary_token.sh`` — the control token for someone SSH'd into the Pi.

A new owner looking at a locked dashboard had no way to learn that the token
lives in /etc/canary/control.token (2026-11-06). The script is run for real
against scratch files; ``pair`` is checked by standing a recording script in
for the venv's python, so no daemon is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "canary_token.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env},
        timeout=30,
    )


def _token_file(tmp_path: Path, value: str = "s3cret-token\n") -> Path:
    path = tmp_path / "control.token"
    path.write_text(value)
    path.chmod(0o640)
    return path


def test_prints_only_the_token_on_stdout(tmp_path: Path) -> None:
    """stdout is just the token, so `$(canary-token)` and pipes get it clean."""
    result = _run(tmp_path, CANARY_CONTROL_TOKEN_FILE=str(_token_file(tmp_path)))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "s3cret-token\n"


def test_a_missing_or_empty_token_file_says_what_to_do(tmp_path: Path) -> None:
    missing = _run(tmp_path, CANARY_CONTROL_TOKEN_FILE=str(tmp_path / "nope"))
    assert missing.returncode == 1 and "install_pi.sh" in missing.stderr
    assert missing.stdout == ""

    empty = _run(tmp_path, CANARY_CONTROL_TOKEN_FILE=str(_token_file(tmp_path, "\n")))
    assert empty.returncode == 1 and "is empty" in empty.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_token_file_names_the_way_in(tmp_path: Path) -> None:
    path = _token_file(tmp_path)
    path.chmod(0o000)
    try:
        result = _run(tmp_path, CANARY_CONTROL_TOKEN_FILE=str(path))
    finally:
        path.chmod(0o640)
    assert result.returncode == 1
    assert "sudo canary-token" in result.stderr


def test_pair_runs_canaryctl_against_the_local_daemon_with_the_token(tmp_path: Path) -> None:
    home = tmp_path / "checkout"
    python = home / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    record = tmp_path / "argv"
    python.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "TOKEN=$CANARY_CONTROL_TOKEN" "$@" > "{record}"\n'
    )
    python.chmod(0o755)
    env_file = tmp_path / "canary.env"
    env_file.write_text(f"CANARY_HOME={home}\n")

    result = _run(
        tmp_path,
        "pair",
        "--yes",
        CANARY_CONTROL_TOKEN_FILE=str(_token_file(tmp_path)),
        CANARY_ENV=str(env_file),
    )
    assert result.returncode == 0, result.stderr
    assert record.read_text().splitlines() == [
        "TOKEN=s3cret-token",
        str(home / "scripts" / "canaryctl.py"),
        "--host",
        "http://localhost:8000",
        "pair",
        "--yes",
    ]


def test_an_unknown_argument_prints_usage(tmp_path: Path) -> None:
    result = _run(tmp_path, "bogus")
    assert result.returncode == 2 and "Usage: canary-token" in result.stderr
