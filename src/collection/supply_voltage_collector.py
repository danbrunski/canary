"""Record the vehicle supply voltage from the CANary-HAT's 12 V sense.

One single-shot ADS1015 conversion per tick, scaled by the configured
divider, emitted as ``sensor:SUPPLY_VOLTAGE`` in volts and stamped at the
conversion. Reads key-off and parked, where ``obd:CONTROL_MODULE_VOLTAGE``
needs an awake ECM. Consumers: the non-volatile write gate's battery check
(H03.2) and, later, cranking and charging diagnostics.

An external-sensor channel: never trains or scores the anomaly detectors
(``docs/roadmap/tasks/H00-hat-integration-README.md``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from src.collection.ads1015 import Ads1015, Ads1015Error
from src.collection.i2c_dev import LinuxI2cBus, RegisterBus
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import (
    DEFAULT_RECONNECT_POLICY,
    ReconnectPolicy,
    run_with_reconnect,
    wait_for_stop_or_timeout,
)
from src.collection.vehicle_power import VEHICLE_POWER
from src.config import SupplyVoltageConfig
from src.telemetry import SENSOR_SOURCE, TelemetryRecord

log = logging.getLogger(__name__)

CHANNEL = "SUPPLY_VOLTAGE"
UNIT = "V"

BusFactory = Callable[[SupplyVoltageConfig], RegisterBus]


def linux_bus_factory(config: SupplyVoltageConfig) -> RegisterBus:
    return LinuxI2cBus(config.i2c_bus, config.i2c_address)


def supply_record(config: SupplyVoltageConfig, stamp: float, pin_volts: float) -> TelemetryRecord:
    return TelemetryRecord.from_epoch(
        stamp,
        source=SENSOR_SOURCE,
        channel=CHANNEL,
        value=pin_volts * config.divider_ratio,
        unit=UNIT,
    )


def read_supply_once(
    config: SupplyVoltageConfig, *, bus_factory: BusFactory = linux_bus_factory
) -> float | None:
    """One reading, for a caller that runs before the collector does (the
    startup VIN probe). Notes it in :data:`VEHICLE_POWER` too. ``None`` when
    the sense is disabled or the read fails: the caller goes on as without it."""
    if not config.enabled:
        return None
    try:
        bus = bus_factory(config)
        try:
            _, pin_volts = Ads1015(bus).read_volts(config.input_channel)
        finally:
            bus.close()
    except (OSError, Ads1015Error):
        log.debug("one-shot supply read failed", exc_info=True)
        return None
    volts = pin_volts * config.divider_ratio
    VEHICLE_POWER.note(volts)
    return volts


async def collect_supply_voltage(
    adc: Ads1015,
    config: SupplyVoltageConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    on_drop = QueueDropCounter("supply_voltage") if drop_counter is None else drop_counter
    period = 1.0 / config.rate_hz
    while not stop_event.is_set():
        stamp, volts = await asyncio.to_thread(adc.read_volts, config.input_channel)
        record = supply_record(config, stamp, volts)
        if isinstance(record.value, float):
            VEHICLE_POWER.note(record.value)
        put_nowait_or_drop(queue, record, on_drop=on_drop)
        await wait_for_stop_or_timeout(stop_event, period)


async def collect_supply_voltage_from_config(
    config: SupplyVoltageConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    bus_factory: BusFactory = linux_bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
) -> None:
    drop_counter = QueueDropCounter("supply_voltage")

    async def _attempt(capture: Callable[[RegisterBus], None]) -> None:
        bus = bus_factory(config)
        capture(bus)
        log.info(
            "supply voltage collection started",
            extra={
                "i2c_bus": config.i2c_bus,
                "i2c_address": f"{config.i2c_address:#04x}",
                "input_channel": config.input_channel,
                "divider_ratio": config.divider_ratio,
            },
        )
        await collect_supply_voltage(
            Ads1015(bus), config, queue, stop_event, drop_counter=drop_counter
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
        reconnect_message="supply voltage collector failed; reconnecting",
        log_extra=lambda: {"i2c_bus": config.i2c_bus, "i2c_address": config.i2c_address},
    )


__all__ = [
    "CHANNEL",
    "collect_supply_voltage",
    "collect_supply_voltage_from_config",
    "supply_record",
]
