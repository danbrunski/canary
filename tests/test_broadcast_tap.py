"""The broadcast tap (R23.9): what both raw-capture paths do with a frame.

``can_collector.CanReader`` (the passive sniffer) and the CAN-OBD collector's
``on_frame`` each skip private traffic (W08.7), tell ``VehiclePower`` the bus
is awake unless the frame is a tester request, and hand off the record. The
site tests below pin that for each path; the unit tests pin the shared tap.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator

import can
import pytest

from src.collection import can_collector, can_obd_collector, private_traffic
from src.collection.can_bus import broadcast_tap, frame_to_record
from src.collection.vehicle_power import VehiclePower
from src.config import CanBusConfig
from src.telemetry import TelemetryRecord
from tests.waits import wait_until

HOLDER = "test-broadcast-tap"
PRIVATE_ID = 0x7E8
ENGINE_ID = 0x1F9


class _Power(VehiclePower):
    """A real ``VehiclePower`` that also counts the bus-awake notes."""

    def __init__(self) -> None:
        super().__init__()
        self.notes = 0

    def note_bus_frame(self, *, at: float | None = None) -> None:
        self.notes += 1
        super().note_bus_frame(at=at)


class _ScriptedBus:
    """Hands out its frames once, then nothing."""

    def __init__(self, frames: list[can.Message]) -> None:
        self.frames = list(frames)
        self.lock = threading.Lock()

    def recv(self, timeout: float) -> can.Message | None:
        with self.lock:
            if self.frames:
                return self.frames.pop(0)
        time.sleep(min(timeout, 0.01))
        return None

    @property
    def drained(self) -> bool:
        with self.lock:
            return not self.frames


def _msg(arbitration_id: int) -> can.Message:
    return can.Message(arbitration_id=arbitration_id, data=bytes(8), timestamp=time.time())


@pytest.fixture
def held() -> Iterator[None]:
    private_traffic.hold(HOLDER, {PRIVATE_ID})
    try:
        yield
    finally:
        private_traffic.release(HOLDER)


#: Private, tester request (physical), tester request (functional), engine.
FRAMES = (PRIVATE_ID, 0x7E0, 0x7DF, ENGINE_ID)


@pytest.mark.asyncio
async def test_the_passive_reader_skips_private_notes_only_modules_and_hands_off(
    monkeypatch: pytest.MonkeyPatch, held: None
) -> None:
    power = _Power()
    monkeypatch.setattr(can_collector, "VEHICLE_POWER", power)
    bus = _ScriptedBus([_msg(i) for i in FRAMES])
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        can_collector.collect_can(bus, queue, stop, poll_interval=0.01)  # type: ignore[arg-type]
    )
    await wait_until(lambda: bus.drained and queue.qsize() == 3, what="three records")
    stop.set()
    stats = await asyncio.wait_for(task, 5)

    ids = [queue.get_nowait().metadata.get("can_id") for _ in range(3)]
    assert ids == [0x7E0, 0x7DF, ENGINE_ID]  # the private frame is not a row
    assert power.notes == 1  # only the engine frame says the bus is awake
    assert stats.frames == 3  # a private frame is not counted as received


def test_the_obd_collectors_tap_skips_private_notes_only_modules_and_hands_off(
    monkeypatch: pytest.MonkeyPatch, held: None
) -> None:
    from can.interfaces.virtual import channels as virtual_channels

    from src.collection.obd_scheduler import CanObdSpec

    power = _Power()
    channel = f"broadcast-tap-{uuid.uuid4().hex}"
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel=channel, bitrate=500000), pids=()
    )

    def fake_loop(
        bus: object, spec: object, *, on_frame: object, stop_event: threading.Event, **_: object
    ) -> None:
        for arbitration_id in FRAMES:
            on_frame(_msg(arbitration_id))  # type: ignore[operator]
        stop_event.wait()

    monkeypatch.setattr(can_obd_collector, "run_can_obd_loop", fake_loop)

    async def run() -> list[TelemetryRecord]:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            can_obd_collector.collect_can_obd_from_config(
                spec,
                queue,
                stop,
                reconnect_policy=None,
                power=power,
            )
        )
        await wait_until(lambda: queue.qsize() >= 3, what="three records")
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)
        virtual_channels.pop(channel, None)
        return [queue.get_nowait() for _ in range(queue.qsize())]

    records = asyncio.run(run())
    assert [r.metadata.get("can_id") for r in records] == [0x7E0, 0x7DF, ENGINE_ID]
    assert power.notes == 1


def test_broadcast_tap_hands_off_the_frame_record_and_tells_power_unless_a_tester_spoke(
    held: None,
) -> None:
    power = _Power()
    pushed: list[TelemetryRecord] = []
    tap = broadcast_tap(power, pushed.append)

    for arbitration_id in FRAMES:
        tap(_msg(arbitration_id))

    assert [r.metadata.get("can_id") for r in pushed] == [0x7E0, 0x7DF, ENGINE_ID]
    assert power.notes == 1
    again = _msg(ENGINE_ID)
    tap(again)
    assert pushed[-1] == frame_to_record(again)
    assert power.notes == 2
