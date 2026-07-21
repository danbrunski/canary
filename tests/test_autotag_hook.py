"""Tests for the live auto-tagger hook (Phase 2a.6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.config import AutoTagConfig
from src.daemon.autotag_hook import LiveAutoTagHook


def _ts(seconds: float) -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _hook(tmp_path: Path, config: AutoTagConfig, *, tick_seconds: float = 1.0) -> LiveAutoTagHook:
    return LiveAutoTagHook(config=config, session_dir=lambda: tmp_path, _tick_seconds=tick_seconds)


def _tags(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "tags.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_disabled_by_default_never_writes(tmp_path: Path) -> None:
    """Hard pre-made decision: live_enabled defaults False."""
    config = AutoTagConfig()
    assert config.live_enabled is False
    hook = _hook(tmp_path, config)
    for i in range(20):
        hook.tick(
            {"gps:speed": 0.0, "obd:SPEED": 0.0, "imu:ACCEL_LON_MAX": 0.9},
            _ts(i),
        )
    assert _tags(tmp_path) == []


def test_stopped_fires_once_on_rising_edge(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    for i in range(5):
        hook.tick({"gps:speed": 40.0}, _ts(i))
    for i in range(5, 10):
        hook.tick({"gps:speed": 0.2}, _ts(i))
    tags = _tags(tmp_path)
    assert [t["label"] for t in tags] == ["stopped"]
    assert tags[0]["source"] == "autotag"


def test_stopped_does_not_refire_while_condition_holds(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    for i in range(10):
        hook.tick({"gps:speed": 0.1}, _ts(i))
    tags = _tags(tmp_path)
    assert len(tags) == 1


def test_stopped_refires_after_condition_drops_and_rises_again(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    hook.tick({"gps:speed": 0.1}, _ts(0))
    hook.tick({"gps:speed": 60.0}, _ts(1))
    hook.tick({"gps:speed": 0.1}, _ts(2))
    tags = _tags(tmp_path)
    assert len(tags) == 2
    assert all(t["label"] == "stopped" for t in tags)


def test_hard_accel_requires_positive_obd_speed_derivative(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    speeds = [0.0, 10.0, 20.0, 30.0]
    for i, speed in enumerate(speeds):
        hook.tick({"obd:SPEED": speed, "imu:ACCEL_LON_MAX": 0.5}, _ts(i))
    tags = _tags(tmp_path)
    assert any(t["label"] == "hard_accel" for t in tags)
    assert not any(t["label"] == "hard_brake" for t in tags)


def test_hard_brake_requires_negative_obd_speed_derivative(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    speeds = [90.0, 70.0, 50.0, 30.0]
    for i, speed in enumerate(speeds):
        hook.tick({"obd:SPEED": speed, "imu:ACCEL_LON_MAX": 0.6}, _ts(i))
    tags = _tags(tmp_path)
    assert any(t["label"] == "hard_brake" for t in tags)
    assert not any(t["label"] == "hard_accel" for t in tags)


def test_cornering_fires_on_lateral_accel_rising_edge(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True, cornering_g=0.3)
    hook = _hook(tmp_path, config)
    hook.tick({"imu:ACCEL_LAT_MAX": 0.1}, _ts(0))
    hook.tick({"imu:ACCEL_LAT_MAX": 0.5}, _ts(1))
    tags = _tags(tmp_path)
    assert [t["label"] for t in tags] == ["cornering"]
    assert tags[0]["peak_g"] == pytest.approx(0.5)


def test_launch_requires_sustained_condition_from_near_stop(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True, launch_sustain_seconds=3.0)
    hook = _hook(tmp_path, config, tick_seconds=1.0)
    # Row 0 never counts toward the streak (no prior sample => no OBD speed
    # derivative yet), so the near-stop run needs one extra row beyond the
    # 3-tick sustain requirement.
    speeds = [0.0, 1.0, 2.0, 3.0, 20.0]
    for i, speed in enumerate(speeds):
        hook.tick({"obd:SPEED": speed, "imu:ACCEL_LON_MAX": 0.5}, _ts(i))
    tags = _tags(tmp_path)
    assert any(t["label"] == "launch" for t in tags)


def test_launch_absent_when_condition_not_sustained(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True, launch_sustain_seconds=10.0)
    hook = _hook(tmp_path, config, tick_seconds=1.0)
    speeds = [0.0, 1.0, 2.0, 20.0, 30.0]
    for i, speed in enumerate(speeds):
        hook.tick({"obd:SPEED": speed, "imu:ACCEL_LON_MAX": 0.5}, _ts(i))
    # hard_accel legitimately fires here — only launch (the 10s-sustain
    # gate) must stay silent given this short a near-stop run.
    assert not any(t["label"] == "launch" for t in _tags(tmp_path))


def test_highway_cruise_requires_sustained_gps_speed(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True, highway_sustain_seconds=4.0)
    hook = _hook(tmp_path, config, tick_seconds=1.0)
    for i in range(8):
        hook.tick({"gps:speed": 100.0}, _ts(i))
    tags = _tags(tmp_path)
    assert any(t["label"] == "highway_cruise" for t in tags)


def test_no_op_with_empty_point(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = _hook(tmp_path, config)
    hook.tick(None, _ts(0))
    hook.tick({}, _ts(1))
    assert _tags(tmp_path) == []


def test_missing_session_dir_is_a_no_op(tmp_path: Path) -> None:
    config = AutoTagConfig(live_enabled=True)
    hook = LiveAutoTagHook(config=config, session_dir=lambda: None)
    hook.tick({"gps:speed": 0.1}, _ts(0))
    # Nothing to assert on disk — just must not raise.
