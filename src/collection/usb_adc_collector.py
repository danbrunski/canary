"""Read analog sensors through a USB serial ADC module.

The module (``hardware.usb_adc``; first: a NOYITO 10-channel STM32F103 + CH340
board) needs no commands: on power-up it prints one text frame per period.
Three firmwares, all read here::

    CH0:0477\t0.382V\r\n      stock: every input every 0.5 s, with volts
    CH0:2114\r\n              firmware/usb-adc v1: configured inputs, 100 Hz
    FW:2 503905 15864\r\n     v2, about once a second: version, rate (mHz),
    CH0:2114\t7342\r\n            group delay (us); then counts and the
    \r\n                          sample's sequence number mod 10000, 503.9 Hz

A blank line ends every frame. Only the counts are used. The stock
firmware's volts column assumes a nominal 3.3 V reference; the catalog
(``data/usb_adc_sensors.yaml``) carries the measured one. Each configured
input becomes a ``sensor:<name>`` record, or a ``sensor:<name>_FAULT`` state
when the sensor is outside its valid window (:mod:`src.decode.analog_sensors`).
The raw line rides along as the payload, so a calibration correction can be
re-applied to stored sessions.

**Timestamps.** Stock and v1 samples are stamped when their line arrived.
v2 samples are stamped by their sequence number, on a clock fitted to the
lower envelope of the arrivals (:class:`SampleClock`: a sample cannot arrive
before it was sent, and USB only ever adds delay), minus the filter's group
delay, so the stamp is the instant the sample represents and the spacing is
exactly the firmware's. Each v2 record carries ``adc_seq``, the unwrapped
sequence number, for analysis that wants the sample index itself. v2 samples
before the first ``FW`` line (up to ~1 s after opening) are dropped: without
it their rate and delay are unknown.

**Stored rate.** v2 delivers 503.9 samples/s. Unless the active capture
profile sets ``analog_full_rate``, each run of :data:`STORED_HZ`-worth of
consecutive samples (5) is stored as one record, their mean, stamped at their
mean instant: the 100 Hz the corpus has always had, from better-filtered
samples. A pulsation capture sets it and stores every sample.

**The first frame after opening is discarded.** It can begin mid-line, and
lines queued before the port was opened report an earlier moment than the
stamp they would get.

The port is read on a thread of its own, which stamps each line as it reads
it: at ~1,000 lines/s, one event-loop hop per line would be a real cost to
everything else the loop runs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from typing import Protocol, cast

import serial

from src.collection import tuning_profile
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import DEFAULT_RECONNECT_POLICY, ReconnectPolicy, run_with_reconnect
from src.config import UsbAdcConfig
from src.decode.analog_sensors import (
    AnalogReading,
    AnalogSensorCatalog,
    decode_counts,
    fault_channel,
    load_analog_sensor_catalog,
)
from src.telemetry import SENSOR_SOURCE, MetadataValue, TelemetryRecord

log = logging.getLogger(__name__)

_LINE = re.compile(rb"^CH(\d{1,2}):(\d{1,5})(?:\t|$)")
_SEQ = re.compile(rb"^CH\d{1,2}:\d{1,5}\t(\d{1,5})$")
_FW = re.compile(rb"^FW:(\d+) (\d+) (\d+)$")

#: v2 prints the sequence number modulo this.
SEQ_MODULUS = 10_000
#: The rate a v2 stream is stored at unless the capture profile asks for every
#: sample: the boxcar of each run of round(rate / STORED_HZ) samples.
STORED_HZ = 100.0
#: Longest a v2 stream is held waiting for its clock (see SampleClock.ready);
#: it is ready ~5 s after a clean start.
MAX_PENDING_SECONDS = 60.0
#: How often the loop processes what the reader thread has gathered.
DRAIN_SECONDS = 0.02


class UsbAdcStalledError(RuntimeError):
    """The module stopped sending parseable lines; the port is reopened."""


class SerialLines(Protocol):
    """The slice of ``serial.Serial`` the collector uses (eases faking in tests)."""

    def readline(self) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


def serial_factory(config: UsbAdcConfig) -> SerialLines:
    """Open the module's port. Nothing is ever written to it."""
    return cast(
        SerialLines,
        serial.Serial(config.port, config.baudrate, timeout=config.read_timeout_seconds),
    )


def parse_line(line: bytes) -> tuple[int, int] | None:
    """``b"CH0:0477\\t0.382V"`` -> ``(0, 477)``; anything else -> ``None``."""
    match = _LINE.match(line)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_sequence(line: bytes) -> int | None:
    """A v2 line's sequence number (``b"CH0:2114\\t7342"`` -> 7342), else ``None``."""
    match = _SEQ.match(line)
    return None if match is None else int(match.group(1))


@dataclass(frozen=True)
class FirmwareInfo:
    """What a v2 ``FW`` line announces."""

    version: int
    rate_hz: float
    group_delay_s: float


def parse_firmware_line(line: bytes) -> FirmwareInfo | None:
    """``b"FW:2 503905 15864"`` -> version 2, 503.905 Hz, 15.864 ms."""
    match = _FW.match(line)
    if match is None:
        return None
    version, rate_mhz, delay_us = (int(g) for g in match.groups())
    if rate_mhz == 0:
        return None
    return FirmwareInfo(version, rate_mhz / 1000.0, delay_us / 1e6)


class SampleClock:
    """Stamps v2 samples from their sequence numbers.

    Sample ``n`` leaves the module at ``a + n * P``. It arrives later by a
    latency that is never negative, so the earliest arrivals trace
    ``a + n * P``: each block of :attr:`BLOCK` samples contributes its
    least-delayed one, and a line under the last :attr:`KEEP` of those gives
    the offset and corrects the nominal period ``P`` (the module's RC
    oscillator is ~1 % off). The stamp is that line minus the filter's group
    delay: the instant the sample represents, not the one it was sent.

    A whole block can arrive late: while the daemon starts, the reader thread
    was starved for seconds on 2026-10-04 and the lines queued in the kernel.
    Those minima lie above the envelope, and a plain least-squares line
    through them made the first ~30 s of stamps run 5-140 % fast. So the fit
    keeps only minima within :attr:`ENVELOPE_S` of its own lower edge, and
    the clock is :attr:`ready` only once that edge rests on enough of them,
    the newest included (the reader has caught up).
    """

    BLOCK = 256
    #: ~12 s of block minima; the fit tries every pair, so keep this small.
    KEEP = 24
    #: Pairs closer than this give too coarse a slope to try.
    PAIR_SPAN_S = 2.0
    #: A block minimum this far above the fitted edge was delayed, not sent late.
    ENVELOPE_S = 0.002
    #: Ready once this many minima, spanning this long, sit on the edge.
    READY_MINIMA = 5
    READY_SPAN_S = 4.0
    #: The RC oscillator's tolerance, with margin: a fitted rate further than
    #: this from nominal is a bad fit, not a fast module.
    MAX_RATE_ERROR = 0.03

    def __init__(self, info: FirmwareInfo) -> None:
        self.info = info
        self.period = 1.0 / info.rate_hz
        self._last_n: int | None = None
        self._last_arrival = 0.0
        self._block: int | None = None
        self._block_min: tuple[int, float] = (0, 0.0)
        self._minima: deque[tuple[int, float]] = deque(maxlen=self.KEEP)
        self._intercept = 0.0
        self._slope = 0.0
        self._ready = False

    def unwrap(self, seq: int, arrival: float) -> int:
        """The full sequence number of a sample printed as ``seq`` mod
        :data:`SEQ_MODULUS`: the one nearest where the elapsed time puts it."""
        if self._last_n is None:
            return seq
        elapsed = max(1, round((arrival - self._last_arrival) / self.period))
        expected = self._last_n + elapsed
        n = expected + ((seq - expected + SEQ_MODULUS // 2) % SEQ_MODULUS - SEQ_MODULUS // 2)
        if n <= self._last_n:
            n += SEQ_MODULUS * ((self._last_n - n) // SEQ_MODULUS + 1)
        return n

    def observe(self, n: int, arrival: float) -> None:
        offset = arrival - n * self.period
        self._last_n, self._last_arrival = n, arrival
        block = n // self.BLOCK
        if block != self._block:
            if self._block is not None:
                self._minima.append(self._block_min)
                self._refit()
            self._block = block
            self._block_min = (n, offset)
        elif offset < self._block_min[1]:
            self._block_min = (n, offset)

    def _refit(self) -> None:
        """The line under every block minimum that the most minima rest on.

        Each pair of minima at least :attr:`PAIR_SPAN_S` apart proposes a
        slope (within the oscillator's tolerance); the line of that slope is
        lowered until no minimum is below it, and scored by how many minima
        lie within :attr:`ENVELOPE_S` of it. Delayed blocks sit above every
        such line, so they cannot tilt the winner: they only fail to score.
        """
        points = list(self._minima)
        if len(points) < 2:
            return
        min_gap = self.PAIR_SPAN_S / self.period
        bound = self.MAX_RATE_ERROR * self.period
        best: tuple[int, float, float, float, list[tuple[int, float]]] | None = None
        for i, (n1, o1) in enumerate(points):
            for n2, o2 in points[i + 1 :]:
                if n2 - n1 < min_gap:
                    continue
                slope = (o2 - o1) / (n2 - n1)
                if abs(slope) > bound:
                    continue
                intercept = min(o - slope * n for n, o in points)
                inliers = [
                    (n, o) for n, o in points if o - (intercept + slope * n) <= self.ENVELOPE_S
                ]
                spread = sum(o - (intercept + slope * n) for n, o in inliers) / len(inliers)
                key = (len(inliers), -spread)
                if best is None or key > (best[0], -best[1]):
                    best = (len(inliers), spread, slope, intercept, inliers)
        if best is None:
            return
        count, _, slope, intercept, inliers = best
        self._slope, self._intercept = slope, intercept
        span = (inliers[-1][0] - inliers[0][0]) * self.period
        newest_on_edge = inliers[-1] == points[-1]
        if count >= self.READY_MINIMA and span >= self.READY_SPAN_S and newest_on_edge:
            self._ready = True

    @property
    def ready(self) -> bool:
        """Whether stamps can be trusted (see the class docstring)."""
        return self._ready

    @property
    def last_n(self) -> int | None:
        """The last sequence number observed, unwrapped."""
        return self._last_n

    @property
    def rate_hz(self) -> float:
        """The measured output rate (nominal until the first fit)."""
        return 1.0 / (self.period + self._slope)

    def stamp(self, n: int) -> float:
        if not self._ready:
            raise RuntimeError("SampleClock.stamp() before the clock is ready")
        sent = n * self.period + self._intercept + self._slope * n
        return sent - self.info.group_delay_s


@dataclass
class _Run:
    """Consecutive v2 samples of one input waiting to be stored as their mean."""

    key: int
    first_seq: int
    counts: list[int] = field(default_factory=list)
    stamps: list[float] = field(default_factory=list)
    raw: list[bytes] = field(default_factory=list)


def counts_to_records(
    catalog: AnalogSensorCatalog,
    adc_input: int,
    counts: float,
    stamp: float,
    raw: bytes,
    extra: dict[str, MetadataValue] | None = None,
) -> list[TelemetryRecord]:
    """The record one input's reading becomes; empty for an unconfigured input."""
    channel = catalog.channel_for_input(adc_input)
    if channel is None:
        return []
    result = decode_counts(catalog.module, channel, counts)
    metadata: dict[str, MetadataValue] = {
        "adc_input": adc_input,
        "adc_counts": counts if isinstance(counts, int) else round(counts, 2),
        "sensor_volts": round(result.sensor_volts, 4),
        **(extra or {}),
    }
    if isinstance(result, AnalogReading):
        return [
            TelemetryRecord.from_epoch(
                stamp,
                source=SENSOR_SOURCE,
                channel=result.name,
                value=result.value,
                unit=result.unit,
                payload=raw,
                metadata=metadata,
            )
        ]
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=SENSOR_SOURCE,
            channel=fault_channel(result.name),
            value=result.kind,
            payload=raw,
            metadata=metadata,
        )
    ]


@dataclass
class UsbAdcStats:
    lines: int = 0
    #: Non-blank lines that were not a reading or a ``FW`` line, dropped.
    parse_errors: int = 0
    #: Readings outside their sensor's valid window (recorded as ``_FAULT``).
    faults: int = 0
    #: v2 samples the sequence numbers say never arrived.
    missed_samples: int = 0
    #: v2 samples dropped while waiting for the first ``FW`` line.
    awaiting_firmware_line: int = 0
    #: v2 samples dropped because the clock never became ready to stamp them
    #: (more than :data:`MAX_PENDING_SECONDS` held).
    untimed_dropped: int = 0
    firmware_version: int | None = None
    #: The v2 output rate measured against the host clock.
    measured_rate_hz: float | None = None


class _Assembler:
    """Turns stamped lines into records: frame sync, v2 clocking, and the
    stored-rate choice."""

    def __init__(
        self,
        catalog: AnalogSensorCatalog,
        stats: UsbAdcStats,
        full_rate: Callable[[], bool],
    ) -> None:
        self.catalog = catalog
        self.stats = stats
        self.full_rate = full_rate
        self.synced = False
        self.clock: SampleClock | None = None
        self.runs: dict[int, _Run] = {}
        self.last_stamp: dict[int, float] = {}
        #: Samples held until the clock is ready, then stamped retroactively:
        #: the sequence numbers are exact, so nothing is lost by waiting.
        self.pending: deque[tuple[int, int, int, bytes]] = deque()

    def line(self, raw_line: bytes, arrival: float) -> tuple[list[TelemetryRecord], bool]:
        """Records for one line, and whether it was a reading (for the stall
        check)."""
        line = raw_line.strip()
        if not self.synced:
            if raw_line and not line:
                self.synced = True  # the blank line that ends a frame
            return [], False
        if not line:
            return [], False
        info = parse_firmware_line(line)
        if info is not None:
            self._firmware(info)
            return [], True
        parsed = parse_line(line)
        if parsed is None:
            self.stats.parse_errors += 1
            return [], False
        self.stats.lines += 1
        adc_input, counts = parsed
        seq = parse_sequence(line)
        if seq is None:
            return counts_to_records(self.catalog, adc_input, counts, arrival, line), True
        if self.clock is None:
            self.stats.awaiting_firmware_line += 1
            return [], True
        n = self.clock.unwrap(seq, arrival)
        last = self.clock.last_n
        if last is not None and n > last + 1:
            self.stats.missed_samples += n - last - 1
        self.clock.observe(n, arrival)
        self.pending.append((adc_input, counts, n, line))
        if not self.clock.ready:
            limit = int(MAX_PENDING_SECONDS * self.clock.info.rate_hz)
            while len(self.pending) > limit:
                self.pending.popleft()
                self.stats.untimed_dropped += 1
            return [], True
        out: list[TelemetryRecord] = []
        while self.pending:
            held_input, held_counts, held_n, held_line = self.pending.popleft()
            stamp = max(self.clock.stamp(held_n), self.last_stamp.get(held_input, float("-inf")))
            self.last_stamp[held_input] = stamp
            out += self._sample(held_input, held_counts, held_n, stamp, held_line)
        return out, True

    def _firmware(self, info: FirmwareInfo) -> None:
        self.stats.firmware_version = info.version
        if self.clock is None or self.clock.info != info:
            self.clock = SampleClock(info)
            self.runs.clear()
            self.stats.untimed_dropped += len(self.pending)
            self.pending.clear()
        else:
            self.stats.measured_rate_hz = round(self.clock.rate_hz, 3)

    def _sample(
        self, adc_input: int, counts: int, n: int, stamp: float, line: bytes
    ) -> list[TelemetryRecord]:
        assert self.clock is not None
        out: list[TelemetryRecord] = []
        if self.full_rate():
            out += self.flush(adc_input)
            out += counts_to_records(self.catalog, adc_input, counts, stamp, line, {"adc_seq": n})
            return out
        per_run = max(1, round(self.clock.info.rate_hz / STORED_HZ))
        key = n // per_run
        run = self.runs.get(adc_input)
        if run is not None and run.key != key:
            out += self.flush(adc_input)
            run = None
        if run is None:
            run = self.runs[adc_input] = _Run(key=key, first_seq=n)
        run.counts.append(counts)
        run.stamps.append(stamp)
        run.raw.append(line)
        if len(run.counts) == per_run:
            out += self.flush(adc_input)
        return out

    def flush(self, adc_input: int) -> list[TelemetryRecord]:
        run = self.runs.pop(adc_input, None)
        if run is None or not run.counts:
            return []
        count = len(run.counts)
        return counts_to_records(
            self.catalog,
            adc_input,
            sum(run.counts) / count,
            sum(run.stamps) / count,
            b"\n".join(run.raw),
            {"adc_seq": run.first_seq, "adc_samples": count},
        )


async def collect_usb_adc(
    reader: SerialLines,
    catalog: AnalogSensorCatalog,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    stall_seconds: float,
    now_fn: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    drop_counter: QueueDropCounter | None = None,
    stats: UsbAdcStats | None = None,
    full_rate: Callable[[], bool] = tuning_profile.analog_full_rate,
) -> None:
    """Read lines from ``reader`` and enqueue records until stopped.

    Raises :class:`UsbAdcStalledError` after ``stall_seconds`` without a parseable
    line, so the reconnect loop reopens the port.
    """
    on_drop = QueueDropCounter("usb_adc") if drop_counter is None else drop_counter
    counters = UsbAdcStats() if stats is None else stats
    assembler = _Assembler(catalog, counters, full_rate)
    lines: SimpleQueue[tuple[bytes, float]] = SimpleQueue()
    halt = threading.Event()
    failures: list[BaseException] = []

    def read() -> None:
        try:
            while not halt.is_set():
                raw = reader.readline()
                if raw:  # b"" is a read timeout, not a line
                    lines.put((raw, now_fn()))
        except BaseException as exc:  # handed to the loop below
            failures.append(exc)

    thread = threading.Thread(target=read, name="usb-adc-reader", daemon=True)
    thread.start()
    last_reading = monotonic()
    try:
        while True:
            # Read stop before draining, so what was read before it is kept.
            stopping = stop_event.is_set()
            # Only what is there now, so one pass always finishes.
            for _ in range(lines.qsize()):
                try:
                    raw, arrival = lines.get_nowait()
                except Empty:
                    break
                records, reading = assembler.line(raw, arrival)
                if reading:
                    last_reading = monotonic()
                for record in records:
                    if isinstance(record.value, str):
                        counters.faults += 1
                    put_nowait_or_drop(queue, record, on_drop=on_drop)
            on_drop.flush_drop_log()
            if stopping:
                return
            if failures:
                raise failures[0]
            if monotonic() - last_reading > stall_seconds:
                raise UsbAdcStalledError(f"no reading from the USB ADC for {stall_seconds:g} s")
            await asyncio.sleep(DRAIN_SECONDS)
    finally:
        halt.set()
        await asyncio.to_thread(thread.join)


async def collect_usb_adc_from_config(
    config: UsbAdcConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[UsbAdcConfig], SerialLines] = serial_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Own the configured module for the daemon queue-producer path.

    The catalog is loaded before the first attempt: a missing or invalid one
    raises instead of retrying, since it would never record anything.
    """
    catalog = load_analog_sensor_catalog(config.sensors)
    drop_counter = QueueDropCounter("usb_adc")

    async def _attempt(capture: Callable[[SerialLines], None]) -> None:
        reader = factory(config)
        capture(reader)
        reader.reset_input_buffer()
        log.info(
            "usb adc collection started",
            extra={
                "port": config.port,
                "channels": {c.name: c.input for c in catalog.channels},
                "reference_volts": catalog.module.reference_volts,
            },
        )
        await collect_usb_adc(
            reader,
            catalog,
            queue,
            stop_event,
            stall_seconds=config.stall_seconds,
            now_fn=now_fn,
            drop_counter=drop_counter,
        )

    def _teardown(reader: SerialLines) -> None:
        try:
            reader.close()
        except OSError:
            pass

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_teardown,
        log=log,
        reconnect_message="usb adc collector failed; reconnecting",
        log_extra=lambda: {"port": config.port},
    )


__all__ = [
    "SEQ_MODULUS",
    "STORED_HZ",
    "FirmwareInfo",
    "SampleClock",
    "SerialLines",
    "UsbAdcStalledError",
    "UsbAdcStats",
    "collect_usb_adc",
    "collect_usb_adc_from_config",
    "counts_to_records",
    "parse_firmware_line",
    "parse_line",
    "parse_sequence",
]
