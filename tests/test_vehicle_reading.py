"""R29.7: ``vehicle_reading`` without a route module, an app or a parquet session.

It was ``dashboard/routes/rom_library.vehicle_reading`` and ``system.py`` imported
it from there. The stored-rows fallback is the dashboard's, so it is an argument
here (:func:`src.dashboard.readers.read_vehicle_reading` binds it; ``test_rom_backup``
drives that through the real parquet read).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.control.vehicle_reading import vehicle_reading
from src.vehicle_state import VehicleState, VehicleStateName


class _Live:
    """``vehicle_state_inputs`` as the daemon's live source reports it."""

    def __init__(self, **overrides: float | None) -> None:
        self.inputs: dict[str, float | None] = {
            "rpm": 0.0,
            "broadcast_rpm": 0.0,
            "coolant_temp": 60.0,
            "run_time": 0.0,
            "obd_freshness_seconds": 1.0,
            "can_freshness_seconds": 0.05,
            **overrides,
        }

    def vehicle_state_inputs(self, now: Any) -> dict[str, float | None]:
        return dict(self.inputs)


def _storage(
    state: VehicleStateName = "no_data",
    *,
    rpm: float | None = None,
    obd_freshness_seconds: float | None = None,
) -> VehicleState:
    return VehicleState(state=state, rpm=rpm, obd_freshness_seconds=obd_freshness_seconds)


def _never() -> VehicleState:
    raise AssertionError("the stored-rows fallback must not run with a live source")


def test_a_live_source_gives_the_broadcast_speed_the_backup_watches() -> None:
    reading = vehicle_reading(live_source=_Live(), replay_controller=None, storage_state=_never)
    assert reading.state == "ignition_only"
    assert reading.broadcast_rpm == 0.0
    assert reading.obd_freshness_seconds == 1.0
    assert reading.watch_unavailable is None


def test_a_stale_broadcast_is_no_watch_and_says_so() -> None:
    live = _Live(can_freshness_seconds=30.0)
    reading = vehicle_reading(live_source=live, replay_controller=None, storage_state=_never)
    assert reading.broadcast_rpm is None
    assert reading.watch_unavailable == "broadcast engine speed absent or stale"


def test_without_a_live_source_the_state_comes_from_storage_and_the_watch_is_unavailable() -> None:
    reading = vehicle_reading(
        live_source=None,
        replay_controller=None,
        storage_state=lambda: _storage("ignition_only", rpm=0.0, obd_freshness_seconds=2.5),
    )
    assert (reading.state, reading.rpm, reading.obd_freshness_seconds) == (
        "ignition_only",
        0.0,
        2.5,
    )
    assert reading.broadcast_rpm is None
    assert reading.watch_unavailable == "no live telemetry source; live inference is not running"


def test_the_stored_rows_are_read_once_per_reading() -> None:
    calls: list[int] = []

    def storage() -> VehicleState:
        calls.append(1)
        return _storage()

    vehicle_reading(live_source=None, replay_controller=None, storage_state=storage)
    assert calls == [1]


def test_the_fallback_cannot_be_left_out() -> None:
    """A caller that forgot it would get no state at all without live inference."""
    with pytest.raises(TypeError, match="storage_state"):
        vehicle_reading(live_source=None, replay_controller=None)  # type: ignore[call-arg]
