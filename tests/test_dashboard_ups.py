"""Tests for src/dashboard/ups.py — the file-backed UPS status reader."""

from __future__ import annotations

from pathlib import Path

from src.dashboard.ups import (
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
    write_keepalive,
)


def test_read_watchdog_status_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_watchdog_status(tmp_path / "does_not_exist") is None


def test_read_watchdog_status_malformed_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "ups_status"
    path.write_text("not json{")
    assert read_watchdog_status(path) is None


def test_read_watchdog_status_non_object_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "ups_status"
    path.write_text("[1, 2, 3]")
    assert read_watchdog_status(path) is None


def test_read_watchdog_status_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "ups_status"
    path.write_text('{"armed": true, "soc": 87.5, "ts": 1700000000.0}')
    status = read_watchdog_status(path)
    assert status == {"armed": True, "soc": 87.5, "ts": 1700000000.0}


def test_read_keepalive_remaining_missing_file_is_zero(tmp_path: Path) -> None:
    assert read_keepalive_remaining(tmp_path / "no_such_file") == 0.0


def test_read_keepalive_remaining_unparseable_is_zero(tmp_path: Path) -> None:
    path = tmp_path / "ups_keepalive"
    path.write_text("garbage")
    assert read_keepalive_remaining(path, now=0.0) == 0.0


def test_read_keepalive_remaining_computes_from_expiry(tmp_path: Path) -> None:
    path = tmp_path / "ups_keepalive"
    path.write_text("1000.0")
    assert read_keepalive_remaining(path, now=900.0) == 100.0
    assert read_keepalive_remaining(path, now=1100.0) == 0.0  # expired, floored at 0


def test_write_keepalive_sets_future_expiry(tmp_path: Path) -> None:
    path = tmp_path / "ups_keepalive"
    expiry = write_keepalive(15.0, path, now=1000.0)
    assert expiry == 1000.0 + 15.0 * 60.0
    assert path.read_text() == str(expiry)
    assert read_keepalive_remaining(path, now=1000.0) == 15.0 * 60.0


def test_write_keepalive_zero_or_negative_cancels(tmp_path: Path) -> None:
    path = tmp_path / "ups_keepalive"
    write_keepalive(15.0, path, now=1000.0)
    expiry = write_keepalive(0.0, path, now=1001.0)
    assert expiry == 0.0
    assert read_keepalive_remaining(path, now=1001.0) == 0.0


def test_write_keepalive_no_leftover_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "ups_keepalive"
    write_keepalive(5.0, path, now=0.0)
    assert not (tmp_path / "ups_keepalive.tmp").exists()


def test_build_power_snapshot_no_watchdog() -> None:
    snap = build_power_snapshot(None, keepalive_remaining_s=42.0, now=1000.0)
    assert snap["watchdog_running"] is False
    assert snap["watchdog_stale"] is None
    assert snap["armed"] is None
    assert snap["keepalive_remaining_s"] == 42.0
    assert snap["status_age_s"] is None


def test_build_power_snapshot_fresh_status() -> None:
    status = {
        "armed": True,
        "input_lost": False,
        "on_battery": False,
        "soc": 91.2,
        "vcell": 4.1,
        "seconds_lost": 0.0,
        "grace_s": 300.0,
        "ts": 995.0,
    }
    snap = build_power_snapshot(status, keepalive_remaining_s=0.0, now=1000.0)
    assert snap["watchdog_running"] is True
    assert snap["watchdog_stale"] is False
    assert snap["soc"] == 91.2
    assert snap["vcell"] == 4.1
    assert snap["status_age_s"] == 5.0


def test_build_power_snapshot_stale_status() -> None:
    status = {"armed": True, "ts": 900.0}
    snap = build_power_snapshot(status, keepalive_remaining_s=0.0, now=1000.0, stale_after_s=10.0)
    assert snap["watchdog_stale"] is True
    assert snap["status_age_s"] == 100.0


def test_build_power_snapshot_missing_ts_reports_no_age() -> None:
    status = {"armed": True}
    snap = build_power_snapshot(status, keepalive_remaining_s=0.0, now=1000.0)
    assert snap["status_age_s"] is None
    assert snap["watchdog_stale"] is False
