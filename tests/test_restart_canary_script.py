"""``scripts/restart_canary.sh`` names an armed passive mode (D03.30).

A restart during an operator's scan-tool session used to bring CANary back
transmitting (2026-09-24). The daemon now restores a persisted arm, and the
restart script says so — or warns loudly when the daemon being restarted does
not persist it. Passive is named, never refused; the capture refusal is
unchanged. ``curl``, ``sudo`` and ``systemctl`` are shims; ``python3`` is this
interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "restart_canary.sh"


def _exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def run_script(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _exec(bin_dir / "curl", "#!/usr/bin/env bash\nprintf '%s' \"${SHIM_STATUS_JSON}\"\n")
    _exec(bin_dir / "sudo", '#!/usr/bin/env bash\nexec "$@"\n')
    _exec(bin_dir / "systemctl", f'#!/usr/bin/env bash\necho "systemctl $*" >>{log}\n')
    (bin_dir / "python3").symlink_to(sys.executable)

    def _run(status: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "SHIM_STATUS_JSON": json.dumps(status),
            "CANARY_STATUS_URL": "http://shim/status",
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
        )

    _run.log = log  # type: ignore[attr-defined]
    return _run


def _passive(**fields: Any) -> dict[str, Any]:
    return {
        "passive": {
            "enabled": True,
            "source": "control",
            "ttl_remaining_seconds": 1234.4,
            "survives_restart": True,
            **fields,
        }
    }


def test_a_persisted_passive_arm_is_named_and_the_restart_proceeds(run_script: Any) -> None:
    result = run_script(_passive())
    assert result.returncode == 0, result.stderr
    assert "passive mode is armed (source control, 1234 s of TTL left)" in result.stdout
    assert "restores it before transmitting" in result.stdout
    assert "systemctl restart canary.service" in run_script.log.read_text()


def test_an_unpersisted_passive_arm_is_a_loud_warning(run_script: Any) -> None:
    """A daemon from before D03.30 (no ``survives_restart``) comes back
    transmitting; the script says so on stderr."""
    status = _passive(ttl_remaining_seconds=None)
    del status["passive"]["survives_restart"]
    result = run_script(status, "--check")
    assert result.returncode == 0
    assert "WARNING: passive mode is armed (source control, no TTL)" in result.stderr
    assert "comes up TRANSMITTING" in result.stderr
    assert "restart safe: no capture is active" in result.stdout


def test_no_passive_no_note(run_script: Any) -> None:
    result = run_script({"passive": {"enabled": False}}, "--check")
    assert result.returncode == 0
    assert "passive" not in result.stdout + result.stderr


def test_the_capture_refusal_still_holds_beside_passive(run_script: Any) -> None:
    status = {
        **_passive(),
        "capture_profile": {"active": True, "name": "tuning", "session_dir": "/s/_capture-x"},
    }
    result = run_script(status)
    assert result.returncode == 3
    assert "refusing: capture 'tuning' is active (/s/_capture-x)" in result.stderr
    assert "passive mode is armed" in result.stdout
    assert not run_script.log.exists()


def test_a_failed_persist_is_a_warning_not_a_promise(
    run_script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate's own snapshot after a write that failed (disk full): the
    script must warn, not say the arm will be restored."""
    from src.collection import tx_gate

    tx_gate.restore_persisted(tmp_path / tx_gate.PASSIVE_STATE_FILENAME)

    def _disk_full(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tx_gate, "atomic_write_text", _disk_full)
    tx_gate.set_passive(True, source="control", ttl_seconds=600)
    result = run_script({"passive": tx_gate.snapshot()}, "--check")
    assert result.returncode == 0
    assert "WARNING: passive mode is armed (source control" in result.stderr
    assert "restores it before transmitting" not in result.stdout
