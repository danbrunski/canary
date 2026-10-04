"""Read analog sensors through a USB serial ADC module.

The module (``hardware.usb_adc``; first: a NOYITO 10-channel STM32F103 + CH340
board) needs no commands: on power-up it prints one text frame per period.
The stock firmware prints every input every 0.5 s; CANary's own
(``firmware/usb-adc``) prints only the configured inputs, averaged, at 100 Hz
and without the volts column. This reads both::

    CH0:0477\t0.382V\r\n
    CH1:1513\t1.218V\r\n
    ...
    CH9:2212\t1.782V\r\n
    \r\n

Only the counts are used. The stock firmware's volts column assumes a nominal
3.3 V reference; the catalog (``data/usb_adc_sensors.yaml``) carries the measured
one. Each configured input becomes a ``sensor:<name>`` record stamped when its
line arrived, or a ``sensor:<name>_FAULT`` state when the sensor is outside its
valid window (:mod:`src.decode.analog_sensors`). The raw line rides along as
the payload, so a calibration correction can be re-applied to stored sessions.

**The first frame after opening is discarded.** It can begin mid-line, and
lines queued before the port was opened report an earlier moment than the
stamp they would get.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

import serial

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


def counts_to_records(
    catalog: AnalogSensorCatalog,
    adc_input: int,
    counts: int,
    stamp: float,
    raw: bytes,
) -> list[TelemetryRecord]:
    """The record one input's reading becomes; empty for an unconfigured input."""
    channel = catalog.channel_for_input(adc_input)
    if channel is None:
        return []
    result = decode_counts(catalog.module, channel, counts)
    metadata: dict[str, MetadataValue] = {
        "adc_input": adc_input,
        "adc_counts": counts,
        "sensor_volts": round(result.sensor_volts, 4),
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
    #: Non-blank lines that were not a ``CHn:counts`` reading, dropped.
    parse_errors: int = 0
    #: Readings outside their sensor's valid window (recorded as ``_FAULT``).
    faults: int = 0


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
) -> None:
    """Read lines from ``reader`` and enqueue records until stopped.

    Raises :class:`UsbAdcStalledError` after ``stall_seconds`` without a parseable
    line, so the reconnect loop reopens the port.
    """
    on_drop = QueueDropCounter("usb_adc") if drop_counter is None else drop_counter
    counters = UsbAdcStats() if stats is None else stats
    synced = False
    last_reading = monotonic()
    while not stop_event.is_set():
        raw = await asyncio.to_thread(reader.readline)
        stamp = now_fn()
        line = raw.strip()
        parsed = parse_line(line) if synced else None
        if parsed is None:
            if monotonic() - last_reading > stall_seconds:
                raise UsbAdcStalledError(f"no reading from the USB ADC for {stall_seconds:g} s")
            if not synced and raw and not line:
                synced = True  # the blank line that ends a frame
            elif synced and line:
                counters.parse_errors += 1
            continue
        last_reading = monotonic()
        counters.lines += 1
        adc_input, counts = parsed
        for record in counts_to_records(catalog, adc_input, counts, stamp, line):
            if isinstance(record.value, str):
                counters.faults += 1
            put_nowait_or_drop(queue, record, on_drop=on_drop)
        if adc_input == 0:
            on_drop.flush_drop_log()


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
    "SerialLines",
    "UsbAdcStalledError",
    "UsbAdcStats",
    "collect_usb_adc",
    "collect_usb_adc_from_config",
    "counts_to_records",
    "parse_line",
]
