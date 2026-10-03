"""Record the HAT's IMU (LSM6DSOX) as ``imu:*`` telemetry.

Drains the chip's FIFO every ``fifo_poll_seconds`` (the blocking I²C transfer
runs in a worker thread) and emits, per sample instant, ``imu:ACCEL_X/Y/Z`` in
g and ``imu:GYRO_X/Y/Z`` in deg/s, stamped with the instant the chip sampled
them — not the instant the daemon read them (``src.collection.lsm6dsox``) —
plus ``imu:TEMP`` in °C at the chip's 1.6 Hz. Body frame, as mounted:
``src.preprocessing.imu_frame`` resolves the vehicle frame.

These are external-sensor channels: they never train or score the anomaly
detectors (operator decision, ``docs/roadmap/tasks/H00-hat-integration-README.md``).

A FIFO overrun (the daemon fell behind by more than the FIFO holds) is logged
with a count and resets the sample timeline until the next timestamp word;
nothing is interpolated across it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.collection.i2c_dev import LinuxI2cBus, RegisterBus
from src.collection.lsm6dsox import FifoBatch, Lsm6dsox
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import (
    DEFAULT_RECONNECT_POLICY,
    ReconnectPolicy,
    run_with_reconnect,
    wait_for_stop_or_timeout,
)
from src.config import ImuConfig
from src.lsm6dsox_settings import ImuSettings
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

ACCEL_CHANNELS = ("ACCEL_X", "ACCEL_Y", "ACCEL_Z")
GYRO_CHANNELS = ("GYRO_X", "GYRO_Y", "GYRO_Z")
ACCEL_UNIT = "g"
GYRO_UNIT = "deg/s"
TEMP_UNIT = "degC"

BusFactory = Callable[[ImuConfig], RegisterBus]


class ImuDevice(Protocol):
    """What the collector needs from a chip driver (:class:`Lsm6dsox`)."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read_fifo(self) -> FifoBatch: ...


def linux_bus_factory(config: ImuConfig) -> RegisterBus:
    return LinuxI2cBus(config.i2c_bus, config.i2c_address)


#: The achieved rate is measured over the samples' own timestamps in this
#: trailing window. Sample time, not read time: a FIFO drained late still dates
#: its samples correctly, where a count over wall clock would read the drain
#: schedule (AGENTS.md, ``achieved_hz_capture``).
RATE_WINDOW_SECONDS = 5.0


@dataclass
class ImuHealth:
    """Cumulative counters and the achieved rate, exposed on ``/status`` (H02.5)."""

    samples: int = 0
    overruns: int = 0
    undated: int = 0
    #: Records the ingest queue refused (``QueueDropCounter``), cumulative.
    queue_dropped: int = 0
    last_sample_at: float | None = None
    #: The ODR the chip was configured for, so the achieved rate has a reference.
    configured_hz: float | None = None
    _recent: deque[float] = field(default_factory=deque, repr=False)

    def observe_samples(self, stamps: Sequence[float]) -> None:
        """Count dated samples and keep the trailing window of their stamps."""
        if not stamps:
            return
        self.samples += len(stamps)
        self.last_sample_at = stamps[-1]
        self._recent.extend(stamps)
        oldest = stamps[-1] - RATE_WINDOW_SECONDS
        while self._recent and self._recent[0] < oldest:
            self._recent.popleft()

    def achieved_hz(self) -> float | None:
        """Samples per second of sample time over the trailing window.

        ``None`` with fewer than two samples in the window: there is no
        interval to divide by, which is not a rate of zero.
        """
        if len(self._recent) < 2:
            return None
        span = self._recent[-1] - self._recent[0]
        if span <= 0:
            return None
        return (len(self._recent) - 1) / span

    def snapshot(self, *, now: float) -> dict[str, Any]:
        """The ``/status.imu`` counters; ``now`` is host epoch seconds."""
        achieved = self.achieved_hz()
        return {
            "age_seconds": (
                None if self.last_sample_at is None else round(now - self.last_sample_at, 1)
            ),
            "configured_hz": self.configured_hz,
            "achieved_hz": None if achieved is None else round(achieved, 1),
            "rate_window_seconds": RATE_WINDOW_SECONDS,
            "samples": self.samples,
            "overruns": self.overruns,
            "undated": self.undated,
            "queue_dropped": self.queue_dropped,
        }


def batch_to_records(batch: FifoBatch) -> list[TelemetryRecord]:
    records: list[TelemetryRecord] = []
    for sample in batch.samples:
        if sample.accel_g is not None:
            records.extend(
                TelemetryRecord.imu(sample.timestamp, name, value, ACCEL_UNIT)
                for name, value in zip(ACCEL_CHANNELS, sample.accel_g, strict=True)
            )
        if sample.gyro_dps is not None:
            records.extend(
                TelemetryRecord.imu(sample.timestamp, name, value, GYRO_UNIT)
                for name, value in zip(GYRO_CHANNELS, sample.gyro_dps, strict=True)
            )
    records.extend(
        TelemetryRecord.imu(temp.timestamp, "TEMP", temp.celsius, TEMP_UNIT)
        for temp in batch.temperatures
    )
    return records


def settings_from_config(config: ImuConfig) -> ImuSettings:
    return ImuSettings(
        odr_hz=config.sample_rate_hz,
        accel_range_g=config.accel_range_g,
        gyro_range_dps=config.gyro_range_dps,
    )


async def collect_imu(
    device: ImuDevice,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_seconds: float,
    health: ImuHealth | None = None,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Start the device and drain it until ``stop_event``; always powers it down."""
    on_drop = QueueDropCounter("imu") if drop_counter is None else drop_counter
    stats = ImuHealth() if health is None else health
    await asyncio.to_thread(device.start)
    try:
        while not stop_event.is_set():
            if await wait_for_stop_or_timeout(stop_event, poll_seconds):
                break
            batch = await asyncio.to_thread(device.read_fifo)
            if batch.overrun:
                stats.overruns += 1
                log.warning(
                    "IMU FIFO overrun: samples lost; timeline resets at the next timestamp",
                    extra={"imu_overruns": stats.overruns},
                )
            stats.undated += batch.undated
            stats.observe_samples([sample.timestamp for sample in batch.samples])
            for record in batch_to_records(batch):
                put_nowait_or_drop(queue, record, on_drop=on_drop)
            stats.queue_dropped = on_drop.dropped
    finally:
        await asyncio.to_thread(device.stop)


async def collect_imu_from_config(
    config: ImuConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    bus_factory: BusFactory = linux_bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    health: ImuHealth | None = None,
) -> None:
    """Own the configured IMU for the daemon queue-producer path.

    A bad setting raises ``ValueError`` immediately (not retried); a bus error
    or a missing device reconnects with backoff, like the GPS collector.
    """
    if config.source != "lsm6dsox":
        raise ValueError(f"no collector for hardware.imu.source={config.source!r}")
    settings = settings_from_config(config)
    drop_counter = QueueDropCounter("imu")
    stats = ImuHealth() if health is None else health
    stats.configured_hz = settings.odr_hz

    async def _attempt(capture: Callable[[RegisterBus], None]) -> None:
        bus = bus_factory(config)
        capture(bus)
        device = Lsm6dsox(bus, settings)
        log.info(
            "IMU collection started",
            extra={
                "imu_source": config.source,
                "i2c_bus": config.i2c_bus,
                "i2c_address": f"{config.i2c_address:#04x}",
                "odr_hz": settings.odr_hz,
            },
        )
        await collect_imu(
            device,
            queue,
            stop_event,
            poll_seconds=config.fifo_poll_seconds,
            health=stats,
            drop_counter=drop_counter,
        )

    def _teardown(bus: RegisterBus) -> None:
        try:
            bus.close()
        except OSError:
            pass

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_teardown,
        log=log,
        reconnect_message="IMU collector failed; reconnecting",
        log_extra=lambda: {"i2c_bus": config.i2c_bus, "i2c_address": config.i2c_address},
    )


__all__ = [
    "ImuDevice",
    "ImuHealth",
    "batch_to_records",
    "collect_imu",
    "collect_imu_from_config",
    "linux_bus_factory",
    "settings_from_config",
]
