"""The HAT's 12 V sense decides whether a vehicle is on the OBD port, and the
OBD collector does not transmit into an empty bus on the bench (2026-10-05:
the VIN probe filled the transmit queue every minute, ENOBUFS, reconnect)."""

from __future__ import annotations

import asyncio
import logging

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
