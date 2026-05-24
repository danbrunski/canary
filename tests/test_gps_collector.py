"""Tests for the serial GPS collector (records emission, read loop, degradation)."""

from __future__ import annotations

import asyncio

import pytest

from src.collection.gps_collector import (
    GpsSpec,
    collect_gps,
    collect_gps_from_config,
    reading_to_records,
)
from src.collection.gps_nmea import GpsReading
from src.collection.reconnect import ReconnectPolicy
from src.telemetry import TelemetryRecord


def _nmea(body: str) -> str:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


def test_reading_to_records_no_fix_emits_only_health_channels() -> None:
    reading = GpsReading(has_fix=False, fix_quality=0, num_satellites=3)
    channels = {record.channel for record in reading_to_records(reading, 1000.0)}
    # No fix → no bogus 0 km/h "stopped"; just receiver health.
    assert channels == {"fix_quality", "num_satellites"}


def test_reading_to_records_with_fix_emits_speed_position_and_health() -> None:
    reading = GpsReading(
        has_fix=True,
        fix_quality=1,
        num_satellites=7,
        hdop=1.1,
        speed_kmh=80.0,
        heading_deg=90.0,
        latitude=39.9,
        longitude=-105.2,
        altitude_m=1600.0,
    )
    records = {record.channel: record for record in reading_to_records(reading, 1000.0)}
    assert set(records) == {
        "fix_quality",
        "num_satellites",
        "hdop",
        "speed",
        "heading",
        "latitude",
        "longitude",
        "altitude",
    }
    assert records["speed"].value == pytest.approx(80.0)
    assert records["speed"].unit == "km/h"
    assert records["latitude"].value == pytest.approx(39.9)
    assert records["altitude"].value == pytest.approx(1600.0)
    assert all(record.source == "gps" for record in records.values())


class _FakeSerial:
    """Serial stand-in: yields canned lines, then sets stop and returns EOF."""

    def __init__(self, lines: list[str], stop: asyncio.Event) -> None:
        self._lines = [f"{line}\r\n".encode() for line in lines]
        self._stop = stop
        self.closed = False

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        self._stop.set()
        return b""

    def close(self) -> None:
        self.closed = True


async def test_collect_gps_flushes_records_on_rmc() -> None:
    stop = asyncio.Event()
    lines = [
        _nmea("GNGGA,181043.00,3953.0000,N,10515.0000,W,1,07,1.2,1600.5,M,,M,,"),
        _nmea("GNRMC,181043.00,A,3953.0000,N,10515.0000,W,54.00,123.4,240526,,,A"),
    ]
    reader = _FakeSerial(lines, stop)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()

    await asyncio.wait_for(collect_gps(reader, queue, stop, now_fn=lambda: 1000.0), timeout=2.0)

    records = {}
    while not queue.empty():
        record = queue.get_nowait()
        records[record.channel] = record
    # GGA merged with RMC, flushed once at the RMC.
    assert records["speed"].value == pytest.approx(100.008, abs=1e-3)
    assert records["num_satellites"].value == 7
    assert records["fix_quality"].value == 1
    assert records["latitude"].value == pytest.approx(39.8833, abs=1e-3)
    assert all(record.source == "gps" for record in records.values())


async def test_collect_gps_from_config_survives_missing_device() -> None:
    """An unplugged GPS must not crash the daemon — open failures reconnect and
    the collector returns cleanly when stopped, with nothing enqueued."""
    stop = asyncio.Event()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    open_attempts = 0

    def boom(spec: GpsSpec) -> _FakeSerial:
        nonlocal open_attempts
        open_attempts += 1
        raise FileNotFoundError("could not open /dev/ttyUSB0")

    async def stopper() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    policy = ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01)
    await asyncio.wait_for(
        asyncio.gather(
            collect_gps_from_config(
                GpsSpec(device="/dev/ttyUSB0"),
                queue,
                stop,
                factory=boom,
                reconnect_policy=policy,
            ),
            stopper(),
        ),
        timeout=3.0,
    )

    assert open_attempts >= 1  # it tried to open the device
    assert queue.empty()  # and degraded silently — no records, no crash


async def test_collect_gps_from_config_returns_immediately_when_already_stopped() -> None:
    stop = asyncio.Event()
    stop.set()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    opened = False

    def factory(spec: GpsSpec) -> _FakeSerial:
        nonlocal opened
        opened = True
        return _FakeSerial([], stop)

    await asyncio.wait_for(
        collect_gps_from_config(GpsSpec(device="x"), queue, stop, factory=factory),
        timeout=2.0,
    )
    assert opened is False
