"""The HAT's 12 V sense decides whether a vehicle is on the OBD port, and the
OBD collector does not transmit into an empty bus on the bench (2026-10-05:
the VIN probe filled the transmit queue every minute, ENOBUFS, reconnect)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from src.collection.ads1015 import Ads1015, Ads1015Error
from src.collection.can_obd_collector import wait_off_the_bench
from src.collection.vehicle_power import FRESH_SECONDS, VehiclePower, classify_supply


def test_the_port_is_a_vehicle_the_bench_or_unknown() -> None:
    assert classify_supply(12.3) == "vehicle"
    assert classify_supply(9.1) == "vehicle"  # a crank dip is still a battery
    assert classify_supply(0.01) == "bench"
    assert classify_supply(5.0) == "unknown"  # explains nothing
    assert classify_supply(None) == "unknown"


def test_a_stale_reading_says_nothing() -> None:
    power = VehiclePower()
    assert power.connection(now=0.0) == "unknown"
    power.note(0.0, at=100.0)
    assert power.connection(now=101.0) == "bench"
    assert power.connection(now=100.0 + FRESH_SECONDS + 1) == "unknown"
    power.reset()
    assert power.latest(now=101.0) is None


async def test_obd_waits_on_the_bench_and_starts_when_a_battery_appears(
    caplog: pytest.LogCaptureFixture,
) -> None:
    power = VehiclePower()
    power.note(0.01)
    stop = asyncio.Event()
    caplog.set_level(logging.INFO, logger="src.collection.can_obd_collector")
    import src.collection.can_obd_collector as collector

    waiting = asyncio.create_task(wait_off_the_bench(stop, power))
    await asyncio.sleep(0.05)
    assert not waiting.done()  # nothing is sent while the port is empty
    power.note(12.4)
    collector_recheck = collector.BENCH_RECHECK_SECONDS
    await asyncio.wait_for(waiting, timeout=collector_recheck + 5)
    messages = [r.getMessage() for r in caplog.records]
    assert any("OBD polling waits" in m for m in messages)
    assert any("OBD polling starts" in m for m in messages)


async def test_a_configured_sense_is_waited_for_before_the_first_attempt() -> None:
    """2026-10-05: the OBD collector started 30 ms before the supply's first
    reading and hit the empty bench bus once per restart."""
    power = VehiclePower()
    power.expect_readings()
    stop = asyncio.Event()
    waiting = asyncio.create_task(wait_off_the_bench(stop, power))
    await asyncio.sleep(0.05)
    assert not waiting.done()  # no reading yet: it does not guess
    power.note(0.0)  # the first reading says bench: still waiting
    await asyncio.sleep(0.2)
    assert not waiting.done()
    stop.set()
    await asyncio.wait_for(waiting, timeout=5)


async def test_without_a_reading_obd_runs_as_before() -> None:
    await asyncio.wait_for(wait_off_the_bench(asyncio.Event(), VehiclePower()), timeout=1)


class _SlowAdc:
    """Answers 'busy' until the clock passes the deadline, then 'done': a
    thread starved between two reads of a finished conversion."""

    def __init__(self) -> None:
        self.reads = 0
        self.now = 0.0

    def write_block(self, register: int, data: bytes) -> None:
        pass

    def read(self, register: int, length: int) -> bytes:
        self.reads += 1
        if register == 0x01:  # config: OS bit set once 'done'
            return (0x8000 if self.reads > 1 else 0x0000).to_bytes(2, "big")
        return (1143 << 4).to_bytes(2, "big")

    def close(self) -> None:
        pass


def test_a_starved_conversion_is_read_once_more_before_failing() -> None:
    bus = _SlowAdc()
    clock = iter([0.0, 1.0, 1.0, 1.0, 1.0])
    adc = Ads1015(bus, clock=lambda: next(clock), sleep=lambda _s: None)  # type: ignore[arg-type]
    _, volts = adc.read_volts(0)
    assert volts > 0

    class _Dead(_SlowAdc):
        def read(self, register: int, length: int) -> bytes:
            return (0).to_bytes(2, "big")

    clock = iter([0.0, 1.0, 1.0, 1.0])
    with pytest.raises(Ads1015Error):
        Ads1015(_Dead(), clock=lambda: next(clock), sleep=lambda _s: None).read_volts(0)  # type: ignore[arg-type]


def test_one_shot_supply_read_notes_the_reading() -> None:
    from src.collection.supply_voltage_collector import read_supply_once
    from src.collection.vehicle_power import VEHICLE_POWER
    from src.config import SupplyVoltageConfig
    from tests.test_supply_voltage import FakeAdc

    VEHICLE_POWER.reset()
    adc = FakeAdc(1143)  # 12.68 V through the HAT's divider
    volts = read_supply_once(
        SupplyVoltageConfig(enabled=True),
        bus_factory=lambda _c: adc,  # type: ignore[arg-type,return-value]
    )
    assert volts == pytest.approx(12.68, abs=0.01)
    assert adc.closed
    assert VEHICLE_POWER.connection() == "vehicle"
    assert read_supply_once(SupplyVoltageConfig(enabled=False)) is None
    VEHICLE_POWER.reset()


def test_the_vin_probe_does_not_transmit_on_the_bench(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from pathlib import Path

    from src.collection import can_bus, supply_voltage_collector, vehicle_probe
    from src.config import CanaryConfig

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    hardware = config.hardware.model_copy(
        update={"can": config.hardware.can.model_copy(update={"interface": "socketcan"})}
    )
    config = config.model_copy(
        update={
            "hardware": hardware,
            "collection": config.collection.model_copy(update={"obd_source": "canable"}),
        }
    )
    monkeypatch.setattr(supply_voltage_collector, "read_supply_once", lambda _c: 0.01)

    def _no_bus(_config: object) -> None:
        raise AssertionError("opened the bus on the bench")

    monkeypatch.setattr(can_bus, "bus_factory", _no_bus)
    caplog.set_level(logging.INFO, logger="src.collection.vehicle_probe")
    probe = vehicle_probe.probe_vehicle_info(config)
    assert probe.vin is None
    assert any("no vehicle on the OBD port" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Parked: the OBD collector sleeps with the bus (operator, 2026-10-05)
# ---------------------------------------------------------------------------


def test_asleep_needs_a_battery_and_a_silent_bus() -> None:
    from src.collection.vehicle_power import ASLEEP_SECONDS, is_tester_request

    power = VehiclePower()
    power.note(12.4, at=0.0)
    assert power.asleep(now=1.0)  # never heard a module
    power.note_bus_frame(at=0.5)
    assert not power.asleep(now=1.0)
    assert power.asleep(now=0.5 + ASLEEP_SECONDS)
    bench = VehiclePower()
    bench.note(0.0, at=0.0)
    assert not bench.asleep(now=1.0)  # the bench is not a sleeping vehicle
    assert not VehiclePower().asleep(now=1.0)  # no 12 V sense: never decides
    assert is_tester_request(0x7DF) and is_tester_request(0x7E0)
    assert not is_tester_request(0x7E8) and not is_tester_request(0x1F9)


async def test_obd_sleeps_while_parked_and_resumes_when_a_module_talks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.collection.can_obd_collector import wait_for_the_bus

    power = VehiclePower()
    power.note(12.4)
    power.note_bus_frame(at=0.0)  # last heard long ago
    stop = asyncio.Event()
    caplog.set_level(logging.INFO, logger="src.collection.can_obd_collector")
    waiting = asyncio.create_task(wait_for_the_bus(stop, power))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    power.note_bus_frame()  # key on: the modules talk
    await asyncio.wait_for(waiting, timeout=5)
    messages = [r.getMessage() for r in caplog.records]
    assert any("OBD polling sleeps" in m for m in messages)
    assert any("OBD polling resumes" in m for m in messages)


async def test_a_failed_transmit_at_key_off_is_sleep_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At key-off the transmit queue fills (ENOBUFS) before the silence lasts
    ASLEEP_SECONDS. With a battery on the port and the bus already quiet, the
    collector sleeps quietly and polls again when the bus wakes."""
    import can

    from src.collection import can_obd_collector
    from src.config import CanBusConfig

    power = VehiclePower()
    power.note(12.4)
    power.note_bus_frame()
    calls: list[str] = []
    stop = asyncio.Event()

    class _Bus:
        def shutdown(self) -> None:
            calls.append("shutdown")

    def fake_loop(bus: object, spec: object, *, stop_event: threading.Event, **_: object) -> None:
        calls.append("poll")
        if calls.count("poll") == 1:
            power._bus_at = time.monotonic() - 2.0  # the modules just went quiet
            raise can.CanOperationError("Failed to transmit: No buffer space available")
        stop_event.wait(5)

    monkeypatch.setattr(can_obd_collector, "run_can_obd_loop", fake_loop)

    class _Spec:
        can = CanBusConfig(interface="socketcan", channel="can0", bitrate=500000)

    caplog.set_level(logging.INFO, logger="src.collection.can_obd_collector")
    task = asyncio.create_task(
        can_obd_collector.collect_can_obd_from_config(
            _Spec(),  # type: ignore[arg-type]
            asyncio.Queue(),
            stop,
            factory=lambda _c: _Bus(),  # type: ignore[arg-type,return-value]
            power=power,
            sleeps_with_bus=True,
        )
    )
    await asyncio.sleep(0.3)
    assert calls == ["poll", "shutdown"]  # asleep: the bus is closed, nothing polls
    power.note_bus_frame()  # key on
    for _ in range(50):
        if calls.count("poll") == 2:
            break
        await asyncio.sleep(0.1)
    assert calls.count("poll") == 2
    stop.set()
    await asyncio.wait_for(task, timeout=10)
    assert not any("reconnecting" in r.getMessage() for r in caplog.records)


def test_the_probe_listens_for_a_module_before_asking() -> None:
    import can

    from src.collection.vehicle_probe import _hear_a_module

    class _Bus:
        def __init__(self, frames: list[int]) -> None:
            self.frames = frames

        def recv(self, timeout: float) -> can.Message | None:
            if self.frames:
                return can.Message(arbitration_id=self.frames.pop(0))
            time.sleep(min(timeout, 0.01))
            return None

    assert _hear_a_module(_Bus([0x1F9]), 0.5)
    # Our own (or a scan tool's) request is not a module talking.
    assert not _hear_a_module(_Bus([0x7DF, 0x7E0]), 0.1)
