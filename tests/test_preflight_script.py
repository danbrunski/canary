"""Tests for scripts/preflight.py pure-logic checks.

The bus-touching checks (frames/obd) need hardware; here we cover the
parsing/decision logic that decides go vs no-go, with an injected command
runner standing in for `ip link`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.preflight import (
    check_can_link,
    check_config,
    check_storage,
    parse_ip_link,
)
from src.config import CanBusConfig


def _proc(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["ip"], returncode=returncode, stdout=stdout)


def _runner(returncode: int, stdout: str = ""):
    def run(_cmd):
        return _proc(returncode, stdout)

    return run


_UP_JSON = (
    '[{"ifname":"can0","flags":["NOARP","UP","LOWER_UP"],"operstate":"UP",'
    '"linkinfo":{"info_kind":"can","info_data":{"bittiming":{"bitrate":500000}}}}]'
)
_DOWN_JSON = '[{"ifname":"can0","flags":["NOARP"],"operstate":"DOWN","linkinfo":{}}]'
_WRONG_BR_JSON = (
    '[{"ifname":"can0","flags":["UP"],"operstate":"UP",'
    '"linkinfo":{"info_data":{"bittiming":{"bitrate":250000}}}}]'
)

_SOCKETCAN = CanBusConfig(interface="socketcan", channel="can0", bitrate=500000)


def test_parse_ip_link_extracts_first_object() -> None:
    info = parse_ip_link(_UP_JSON)
    assert info is not None
    assert info["ifname"] == "can0"
    assert parse_ip_link("not json") is None
    assert parse_ip_link("[]") is None


def test_check_can_link_up_at_configured_bitrate_passes() -> None:
    r = check_can_link(_SOCKETCAN, runner=_runner(0, _UP_JSON))
    assert r.ok and r.critical
    assert "UP" in r.detail and "500000" in r.detail


def test_check_can_link_down_fails_with_actionable_remediation() -> None:
    r = check_can_link(_SOCKETCAN, runner=_runner(0, _DOWN_JSON))
    assert not r.ok and r.critical
    assert r.remediation is not None and "ip link" in r.remediation


def test_check_can_link_missing_interface_fails() -> None:
    r = check_can_link(_SOCKETCAN, runner=_runner(1, ""))
    assert not r.ok
    assert "does not exist" in r.detail


def test_check_can_link_bitrate_mismatch_fails() -> None:
    r = check_can_link(_SOCKETCAN, runner=_runner(0, _WRONG_BR_JSON))
    assert not r.ok
    assert "250000" in r.detail and "500000" in r.detail


def test_check_can_link_non_socketcan_is_noncritical_skip() -> None:
    slcan = CanBusConfig(interface="slcan", channel="/dev/ttyACM0", bitrate=500000)
    r = check_can_link(slcan, runner=_runner(1, ""))
    assert r.ok and not r.critical


def test_check_storage_pass_and_low_space_fail(tmp_path: Path) -> None:
    ok = check_storage(tmp_path / "sessions", min_free_mb=1)
    assert ok.ok and (tmp_path / "sessions").is_dir()

    low = check_storage(tmp_path / "sessions", min_free_mb=10**12)
    assert not low.ok and low.remediation is not None


def test_check_config_missing_file_fails(tmp_path: Path) -> None:
    result, config = check_config(tmp_path / "nope.yaml")
    assert not result.ok and config is None
    assert result.remediation is not None and "canary.dev.example.yaml" in result.remediation


def test_check_config_loads_dev_example() -> None:
    # The tracked dev template must always be a valid config.
    result, config = check_config(Path("canary.dev.example.yaml"))
    assert result.ok and config is not None
    assert config.hardware.can.interface == "socketcan"
