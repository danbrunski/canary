"""USB ADC firmware v2 (firmware/usb-adc: 503.9 Hz, anti-alias filtered, with
sequence numbers): the collector stamps samples by sequence on a clock fitted
to the earliest arrivals, stores 5-sample means unless the capture profile
asks for every sample, and counts the samples a gap lost."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable
from pathlib import Path

import pytest

from src.collection.usb_adc_collector import (
    SEQ_MODULUS,
    FirmwareInfo,
    SampleClock,
    UsbAdcStats,
    collect_usb_adc,
    parse_firmware_line,
    parse_line,
    parse_sequence,
)
from src.decode.analog_sensors import load_analog_sensor_catalog
from src.telemetry import TelemetryRecord

REPO = Path(__file__).resolve().parents[1]
CATALOG = load_analog_sensor_catalog(REPO / "data/usb_adc_sensors.yaml")
INFO = FirmwareInfo(version=2, rate_hz=503.905, group_delay_s=0.015864)


def test_v2_lines_parse_and_old_collectors_still_read_the_counts() -> None:
    assert parse_line(b"CH0:2114\t7342") == (0, 2114)
    assert parse_sequence(b"CH0:2114\t7342") == 7342
    assert parse_sequence(b"CH0:0477\t0.382V") is None  # stock: volts, not a sequence
    assert parse_sequence(b"CH0:2114") is None  # v1
    assert parse_firmware_line(b"FW:2 503905 15864") == INFO
    assert parse_firmware_line(b"CH0:2114") is None


def test_unwrap_follows_the_sequence_across_the_modulus() -> None:
    clock = SampleClock(INFO)
    t = 1000.0
    n = SEQ_MODULUS - 3
    clock.observe(clock.unwrap(n, t), t)
    for step in range(1, 8):
        t += clock.period
        n_full = clock.unwrap((SEQ_MODULUS - 3 + step) % SEQ_MODULUS, t)
        assert n_full == SEQ_MODULUS - 3 + step
        clock.observe(n_full, t)
    # A gap of 50 samples, 50 periods later, is not mistaken for 1 or 10,050.
    t += 50 * clock.period
    assert clock.unwrap((SEQ_MODULUS + 4 + 50) % SEQ_MODULUS, t) == SEQ_MODULUS + 54


def test_the_clock_recovers_the_true_rate_and_send_instant_from_late_arrivals() -> None:
    # The module's RC runs 0.7 % fast; every arrival is 0.5-6 ms late, in bursts.
    rng = random.Random(4)
    true_period = 1 / (INFO.rate_hz * 1.007)
    t0 = 1_790_000_000.0
    clock = SampleClock(INFO)
    errors = []
    for n in range(60 * 504):
        sent = t0 + n * true_period
        arrival = sent + 0.0005 + rng.expovariate(1 / 0.002) + (0.004 if n % 8 else 0.0)
        clock.observe(clock.unwrap(n % SEQ_MODULUS, arrival), arrival)
        if n > 20 * 504:
            errors.append(clock.stamp(n) + INFO.group_delay_s - sent)
    assert clock.rate_hz == pytest.approx(INFO.rate_hz * 1.007, rel=1e-4)
    # The stamp sits at the least-delayed arrival: within a millisecond of the
    # send instant, never late by the burst latency.
    assert max(abs(e) for e in errors) < 0.001


def _v2_lines(counts: Iterable[int], start: int = 0, fw_every: int = 504) -> list[bytes]:
    out: list[bytes] = [b"\r\n"]  # the sync point
    for n, c in enumerate(counts, start):
        if n % fw_every == 0:
            out.append(b"FW:2 503905 15864\r\n")
        out.append(f"CH0:{c:04d}\t{n % SEQ_MODULUS}\r\n".encode())
        out.append(b"\r\n")
    return out


class FakeSerial:
    def __init__(self, lines: list[bytes], stop: asyncio.Event) -> None:
        self.lines = lines
        self.stop = stop

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        self.stop.set()
        return b""

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


async def _collect(
    lines: list[bytes], *, full_rate: bool, stats: UsbAdcStats | None = None
) -> list[TelemetryRecord]:
    stop = asyncio.Event()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    clock = iter(1_790_000_000.0 + i / 503.905 for i in range(100_000))
    await collect_usb_adc(
        FakeSerial(lines, stop),
        CATALOG,
        queue,
        stop,
        stall_seconds=60.0,
        now_fn=lambda: next(clock),
        stats=stats,
        full_rate=lambda: full_rate,
    )
    out = []
    while not queue.empty():
        out.append(await queue.get())
    return out


async def test_normal_collection_stores_the_mean_of_each_five_samples() -> None:
    counts = [2100, 2110, 2120, 2130, 2140] * 4
    records = await _collect(_v2_lines(counts), full_rate=False)
    assert len(records) == 4
    assert {r.metadata["adc_counts"] for r in records} == {2120.0}
    assert [r.metadata["adc_seq"] for r in records] == [0, 5, 10, 15]
    assert {r.metadata["adc_samples"] for r in records} == {5}
    gaps = [
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(records, records[1:], strict=False)
    ]
    assert gaps == pytest.approx([5 / 503.905] * 3, abs=1e-4)


async def test_a_pulsation_capture_stores_every_sample_by_sequence() -> None:
    counts = [2114 + (n % 7) for n in range(30)]
    records = await _collect(_v2_lines(counts), full_rate=True)
    assert [r.metadata["adc_seq"] for r in records] == list(range(30))
    assert [r.metadata["adc_counts"] for r in records] == counts
    assert records[0].payload == b"CH0:2114\t0"


async def test_samples_before_the_firmware_line_are_dropped_and_gaps_counted() -> None:
    lines = _v2_lines(range(2000, 2030), start=1)  # no FW line until seq 504
    stats = UsbAdcStats()
    assert await _collect(lines, full_rate=True, stats=stats) == []
    assert stats.awaiting_firmware_line == 30

    lines = _v2_lines([2114] * 20)
    del lines[2 * 6 + 2 : 2 * 9 + 2]  # samples 6, 7, 8 never arrive
    stats = UsbAdcStats()
    records = await _collect(lines, full_rate=True, stats=stats)
    assert stats.missed_samples == 3
    assert [r.metadata["adc_seq"] for r in records][5:7] == [5, 9]
