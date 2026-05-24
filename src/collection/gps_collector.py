"""Serial NMEA GPS collection.

Owns the read-only path from a serial GPS receiver to ``gps:*``
``TelemetryRecord``s on the daemon queue. Mirrors the CAN/OBD collectors:
``collect_gps_from_config`` opens the device, runs the read loop, and rebuilds
the connection with bounded backoff if the USB serial link drops.

Sentence parsing lives in :mod:`src.collection.gps_nmea` (pure, unit-tested).
This module is the thin I/O + emission shell: one set of records is flushed per
RMC sentence (one fix epoch, ~1 Hz), carrying ``gps:speed`` (km/h) plus
position/heading and the fix-health channels.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

import serial

from src.collection.gps_nmea import GpsReading, sentence_type, update_reading
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import (
    ReconnectPolicy,
    wait_for_stop_or_timeout,
)
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

DEFAULT_RECONNECT_POLICY = ReconnectPolicy()


@dataclass(frozen=True)
class GpsSpec:
    """How to open the serial GPS receiver."""

    device: str
    baudrate: int = 9600
    read_timeout_seconds: float = 2.0


class SerialReader(Protocol):
    """The slice of ``serial.Serial`` the collector uses (eases faking in tests)."""

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


def serial_factory(spec: GpsSpec) -> SerialReader:
    """Open the configured serial device. Read-only; the GPS is input-only."""
    return cast(
        SerialReader,
        serial.Serial(spec.device, spec.baudrate, timeout=spec.read_timeout_seconds),
    )


def reading_to_records(reading: GpsReading, timestamp: float) -> list[TelemetryRecord]:
    """Convert a completed fix epoch into ``gps:*`` telemetry records.

    Fix-health channels (``fix_quality``, ``num_satellites``, ``hdop``) are
    emitted whenever known — even with no fix — so the dashboard can show "no
    fix, N sats acquired". Position/speed/heading are emitted only with a valid
    fix, so a no-fix epoch never injects a bogus 0 km/h that would read as
    "stopped"; the segmenter then falls back to ``obd:SPEED``.
    """
    records: list[TelemetryRecord] = []

    def add(channel: str, value: float | int, unit: str) -> None:
        records.append(TelemetryRecord.gps(timestamp, channel, value, unit))

    if reading.fix_quality is not None:
        add("fix_quality", int(reading.fix_quality), "")
    if reading.num_satellites is not None:
        add("num_satellites", int(reading.num_satellites), "")
    if reading.hdop is not None:
        add("hdop", float(reading.hdop), "")

    if reading.has_fix:
        if reading.speed_kmh is not None:
            add("speed", float(reading.speed_kmh), "km/h")
        if reading.heading_deg is not None:
            add("heading", float(reading.heading_deg), "deg")
        if reading.latitude is not None:
            add("latitude", float(reading.latitude), "deg")
        if reading.longitude is not None:
            add("longitude", float(reading.longitude), "deg")
        if reading.altitude_m is not None:
            add("altitude", float(reading.altitude_m), "m")

    return records


async def collect_gps(
    reader: SerialReader,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    now_fn: Callable[[], float] = time.time,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Read NMEA lines from ``reader`` and enqueue ``gps:*`` records until stopped.

    One record set is flushed per RMC sentence (the per-epoch "recommended
    minimum"), after merging any GGA/VTG seen earlier in the same burst. The
    blocking ``readline`` runs in a worker thread so the daemon event loop keeps
    serving the dashboard; the serial read timeout bounds shutdown latency.
    """
    on_drop = QueueDropCounter("gps") if drop_counter is None else drop_counter
    reading = GpsReading()
    while not stop_event.is_set():
        raw = await asyncio.to_thread(reader.readline)
        if not raw:
            await asyncio.sleep(0)
            continue
        line = raw.decode("ascii", "ignore").strip()
        if not line.startswith("$"):
            await asyncio.sleep(0)
            continue
        kind = sentence_type(line)
        update_reading(reading, line)
        if kind == "RMC":
            timestamp = now_fn()
            for record in reading_to_records(reading, timestamp):
                put_nowait_or_drop(queue, record, on_drop=on_drop)
            reading = GpsReading()
        await asyncio.sleep(0)


async def collect_gps_from_config(
    spec: GpsSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[GpsSpec], SerialReader] = serial_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Own a configured serial GPS for the daemon queue-producer path."""
    delay = reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0
    drop_counter = QueueDropCounter("gps")

    while not stop_event.is_set():
        reader: SerialReader | None = None
        try:
            reader = factory(spec)
            log.info(
                "gps collection started",
                extra={"device": spec.device, "baudrate": spec.baudrate},
            )
            await collect_gps(
                reader,
                queue,
                stop_event,
                now_fn=now_fn,
                drop_counter=drop_counter,
            )
            return
        except Exception as exc:  # noqa: BLE001 — serial drops must reconnect, not crash the daemon
            if stop_event.is_set():
                return
            if reconnect_policy is None:
                raise
            log.warning(
                "gps collector failed; reconnecting",
                extra={
                    "device": spec.device,
                    "error": f"{type(exc).__name__}: {exc}",
                    "reconnect_delay_seconds": delay,
                },
            )
            if await wait_for_stop_or_timeout(stop_event, delay):
                return
            delay = reconnect_policy.next_delay(delay)
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001 — best-effort close on a possibly-dead handle
                    pass
