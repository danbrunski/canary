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


T0 = 1_790_000_000.0
PERIOD = 1 / INFO.rate_hz
#: Long enough for the clock to become ready (SampleClock.READY_SPAN_S).
SECONDS = 8


def _v2_lines(
    counts: Iterable[int], start: int = 0, fw_every: int = 504
) -> list[tuple[bytes, float]]:
    """Each line with its arrival: the sample's send instant plus 1 ms."""
    out: list[tuple[bytes, float]] = [(b"\r\n", T0)]  # the sync point
    for n, c in enumerate(counts, start):
        arrival = T0 + n * PERIOD + 0.001
        if n % fw_every == 0:
            out.append((b"FW:2 503905 15864\r\n", arrival))
        out.append((f"CH0:{c:04d}\t{n % SEQ_MODULUS}\r\n".encode(), arrival))
        out.append((b"\r\n", arrival))
    return out


class FakeSerial:
    def __init__(self, lines: list[tuple[bytes, float]], stop: asyncio.Event) -> None:
        self.lines = lines
        self.stop = stop
        self.arrival = T0

    def readline(self) -> bytes:
        if self.lines:
            line, self.arrival = self.lines.pop(0)
            return line
        self.stop.set()
        return b""

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        pass


async def _collect(
    lines: list[tuple[bytes, float]], *, full_rate: bool, stats: UsbAdcStats | None = None
) -> list[TelemetryRecord]:
    stop = asyncio.Event()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    reader = FakeSerial(lines, stop)
    await collect_usb_adc(
        reader,
        CATALOG,
        queue,
        stop,
        stall_seconds=60.0,
        now_fn=lambda: reader.arrival,
        stats=stats,
        full_rate=lambda: full_rate,
    )
    out = []
    while not queue.empty():
        out.append(await queue.get())
    return out


async def test_normal_collection_stores_the_mean_of_each_five_samples() -> None:
    samples = SECONDS * 504
    counts = [2100, 2110, 2120, 2130, 2140] * (samples // 5)
    records = await _collect(_v2_lines(counts), full_rate=False)
    # Held until the clock was ready, then every run stamped and stored.
    assert len(records) == len(counts) // 5
    assert {r.metadata["adc_counts"] for r in records} == {2120.0}
    assert [r.metadata["adc_seq"] for r in records[:3]] == [0, 5, 10]
    assert {r.metadata["adc_samples"] for r in records} == {5}
    first = records[0].timestamp.timestamp()
    # Stamped at the mean instant of its five samples, less the filter delay.
    assert first == pytest.approx(T0 + 2 * PERIOD - INFO.group_delay_s, abs=1e-3)
    gaps = [
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(records, records[1:], strict=False)
    ]
    assert max(abs(g - 5 * PERIOD) for g in gaps) < 2e-5


async def test_a_pulsation_capture_stores_every_sample_by_sequence() -> None:
    counts = [2114 + (n % 7) for n in range(SECONDS * 504)]
    records = await _collect(_v2_lines(counts), full_rate=True)
    assert [r.metadata["adc_seq"] for r in records] == list(range(len(counts)))
    assert [r.metadata["adc_counts"] for r in records] == counts
    assert records[0].payload == b"CH0:2114\t0"


async def test_samples_before_the_firmware_line_are_dropped_and_gaps_counted() -> None:
    lines = _v2_lines(range(2000, 2030), start=1)  # no FW line until seq 504
    stats = UsbAdcStats()
    assert await _collect(lines, full_rate=True, stats=stats) == []
    assert stats.awaiting_firmware_line == 30

    lines = _v2_lines([2114] * (SECONDS * 504))
    del lines[2 * 6 + 2 : 2 * 9 + 2]  # samples 6, 7, 8 never arrive
    stats = UsbAdcStats()
    records = await _collect(lines, full_rate=True, stats=stats)
    assert stats.missed_samples == 3
    assert [r.metadata["adc_seq"] for r in records][5:7] == [5, 9]


def test_a_starved_start_is_stamped_from_the_envelope_not_the_backlog() -> None:
    """2026-10-04: the reader thread was starved for seconds while the daemon
    started, and the queued lines all arrived late at once. The clock must not
    be ready on them, and once it is, the held samples' stamps are right."""
    clock = SampleClock(INFO)
    rng = random.Random(9)
    stamps_ready_at = None
    n_total = 12 * 504
    for n in range(n_total):
        sent = T0 + n * PERIOD
        if sent < T0 + 3.0:
            # Starved: everything sent in the first 3 s arrives in a rush after it.
            arrival = T0 + 3.0 + n * PERIOD * 0.05
        else:
            arrival = sent + 0.0005 + rng.expovariate(1 / 0.002)
        clock.observe(clock.unwrap(n % SEQ_MODULUS, arrival), arrival)
        if clock.ready and stamps_ready_at is None:
            stamps_ready_at = sent
    assert stamps_ready_at is not None and stamps_ready_at > T0 + 3.0 + 4.0
    # Every sample, the starved ones included, stamped within a millisecond.
    for n in range(0, n_total, 97):
        assert clock.stamp(n) + INFO.group_delay_s == pytest.approx(T0 + n * PERIOD, abs=1e-3)
