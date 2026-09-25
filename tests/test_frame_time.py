"""One frame-timestamp policy for every receive path (roadmap R02.4).

Three policies stamped received frames: the broadcast path took the driver's
stamp raw, the OBD path used it only within ``KERNEL_STAMP_MAX_SKEW_SECONDS``
of the wall clock, and the sensor bus took anything positive. After a
suspend-stale SocketCAN stamp a broadcast row and an OBD reading from the same
instant were stamped minutes apart, which breaks the same-instant pairing
``compare_channels`` depends on.
"""

from __future__ import annotations

import time

import can
import pytest

from src.collection import can_obd_collector
from src.collection.can_collector import frame_to_record
from src.collection.sensor_can_collector import frame_to_records
from src.decode.sensor_can import load_sensor_can_catalog
from src.paths import REPO_ROOT

NOW = 1_800_000_000.0
AEM_ID = 0x000A0301


@pytest.fixture
def frozen_wall_clock(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(time, "time", lambda: NOW)
    return NOW


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        (NOW - 600.0, NOW),  # suspend-stale SocketCAN stamp: not trusted
        (0.0, NOW),  # an adapter with no clock
        (NOW - 0.010, NOW - 0.010),  # a sane driver stamp is the acquisition clock
    ],
)
def test_broadcast_obd_and_sensor_paths_stamp_a_frame_identically(
    frozen_wall_clock: float, stamp: float, expected: float
) -> None:
    broadcast = can.Message(timestamp=stamp, arbitration_id=0x280, data=bytes(8))
    sensor = can.Message(
        timestamp=stamp, arbitration_id=AEM_ID, is_extended_id=True, data=(340).to_bytes(2, "big")
    )
    catalog = load_sensor_can_catalog(REPO_ROOT / "data/sensor_can_signals.yaml")

    obd_at = can_obd_collector._frame_arrival(broadcast).at
    broadcast_at = frame_to_record(broadcast).timestamp.timestamp()
    (sensor_record,) = frame_to_records(catalog, sensor)

    assert obd_at == pytest.approx(expected, abs=1e-6)
    assert broadcast_at == pytest.approx(obd_at, abs=1e-6)
    assert sensor_record.timestamp.timestamp() == pytest.approx(obd_at, abs=1e-6)
