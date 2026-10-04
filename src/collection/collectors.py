"""Default collector-starter set for the live daemon (Task 4.7, refactoring.md).

``CollectorStarter`` was defined twice (``src/main.py`` and
``src/collection/replay.py``) — one canonical alias here, both re-export it.

``default_collectors``/``_gps_collectors`` (config → concrete collector
coroutines for CAN/OBD/GPS) used to live in ``src/main.py``, with
``src/daemon/runtime.py`` reaching back into it via ``from src.main import
_default_collectors`` — a daemon → main import inversion (``main.py`` is the
CLI entry point; it should depend on the daemon package, not the other way
around). This module is their new home, alongside the collector
constructors they call. (``main.run_collection``, the ``--collection-only``
path that also used them, was retired 2026-09-25, R03.11.)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.collection import tuning_profile
from src.collection.can_collector import ReceiveStatsCallback, collect_can_from_config
from src.collection.can_obd_collector import (
    CanObdSpec,
    ObdSchedulerMetrics,
    collect_can_obd_from_config,
)
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.gps_collector import GpsSpec, collect_gps_from_config
from src.collection.gps_gpsd import GpsdSpec, collect_gpsd_from_config
from src.collection.imu_collector import ImuHealth, collect_imu_from_config
from src.collection.kline_collector import KlineCollector
from src.collection.obd_scheduler import (
    ForcePollTrigger,
    FreezeFrameTrigger,
    did_alias_table,
    profile_did_union,
)
from src.collection.queueing import CollectorHandoffStats
from src.collection.reconnect import ReconnectPolicy
from src.collection.sensor_can_collector import collect_sensor_can_from_config
from src.collection.supply_voltage_collector import collect_supply_voltage_from_config
from src.collection.usb_adc_collector import collect_usb_adc_from_config
from src.config import CanaryConfig
from src.decode.analog_sensors import load_analog_sensor_catalog
from src.decode.sensor_can import load_sensor_can_catalog
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import active_registry
from src.vehicle.tune_revisions import RegistryConflictError

if TYPE_CHECKING:
    from src.collection.obd_collector import ObdSpec

log = logging.getLogger(__name__)

CollectorStarter = Callable[
    ["asyncio.Queue[TelemetryRecord]", asyncio.Event], Coroutine[Any, Any, None]
]


def default_collectors(
    config: CanaryConfig,
    *,
    obd_scheduler_metrics_callback: Callable[[ObdSchedulerMetrics], None] | None = None,
    freeze_frame_trigger: FreezeFrameTrigger | None = None,
    force_poll_trigger: ForcePollTrigger | None = None,
    can_receive_stats_callback: ReceiveStatsCallback | None = None,
    obd_handoff_stats_callback: Callable[[CollectorHandoffStats], None] | None = None,
    imu_health: ImuHealth | None = None,
) -> list[CollectorStarter]:
    """Config -> collector starters.

    ``can_receive_stats_callback`` (C07.5) receives the passive CAN receive
    path's cumulative counters — used by the two branches that run the
    standalone sniffer (OBD ``disabled``, and legacy ELM327 beside passive
    CAN); the ``canable`` branch's combined collector has its own loop.
    ``imu_health`` (H02.5) is the record the IMU collector keeps its counters
    in, so ``/status`` can read them.
    """
    capture_store = CaptureProfileStore(
        config.daemon.capture_profiles,
        config.collection,
        config.daemon.behavior_models,
        custom_pids_path=config.daemon.custom_pids,
    )
    capture_catalog = capture_store.load()
    default_capture = capture_catalog.by_name("default")
    assert default_capture is not None
    # Runtime state is intentionally reset on each daemon start (capture
    # profiles are persistent; the *active capture* is not).
    tuning_profile.configure_default(default_capture)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(config.collection, capture_catalog.profiles))
    )
    reconnect_policy = ReconnectPolicy(
        initial_delay_seconds=config.collection.reconnect_initial_delay_seconds,
        max_delay_seconds=config.collection.reconnect_max_delay_seconds,
    )
    # GPS runs alongside whatever OBD/CAN source is configured (it's an
    # independent serial device), so it's appended to every branch's collector
    # list rather than gated on obd_source.
    # Add-on sensors likewise: a separate CAN interface, independent of which
    # vehicle-bus path is configured. The HAT's IMU is its own I²C device.
    aux_collectors = [
        *_gps_collectors(config, reconnect_policy),
        *_sensor_collectors(config, reconnect_policy),
        *_imu_collectors(config, reconnect_policy, health=imu_health),
        *_supply_voltage_collectors(config, reconnect_policy),
        *_usb_adc_collectors(config, reconnect_policy),
        *_kline_collectors(config),
    ]

    if config.collection.obd_source == "canable":
        # The operational registry (a flash recorded through the daemon, W06.5)
        # wins over the tracked profile copy, same as /control/calibration.
        try:
            registry = active_registry(config)
        except RegistryConflictError as exc:
            # Collection does not need the flash history; /status carries the
            # refusal as a calibration alert (R05.17).
            log.error("running tune revision unknown: %s", exc)
            registry = None
        current = None if registry is None else registry.at(datetime.now(UTC))
        tuning_profile.configure_running_tune_revision(
            None if current is None else current.revision,
            calibration_id=None if current is None else current.calibration_id,
        )
        # The request plan is the saved-profile union; the runtime gate keeps
        # default polling identical while making a profile switch
        # restart-free. Custom PIDs (Task 2.6) are loaded once, not per tick.
        can_obd_spec = CanObdSpec.from_config(
            config.collection,
            config.hardware.can,
            catalog=capture_catalog.profiles,
            custom_pids_path=config.daemon.custom_pids,
            vehicle=config.vehicle,
            freeze_frame_trigger=freeze_frame_trigger,
            force_poll_trigger=force_poll_trigger,
        )

        async def canable_obd(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
            await collect_can_obd_from_config(
                can_obd_spec,
                queue,
                stop,
                reconnect_policy=reconnect_policy,
                on_scheduler_metrics=obd_scheduler_metrics_callback,
                on_handoff_stats=obd_handoff_stats_callback,
            )

        return [canable_obd, *aux_collectors]

    async def can(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_can_from_config(
            config.hardware.can,
            queue,
            stop,
            reconnect_policy=reconnect_policy,
            on_receive_stats=can_receive_stats_callback,
        )

    if config.collection.obd_source == "disabled":
        return [can, *aux_collectors]

    # The ELM327 transport's own library (python-OBD, and pint through it,
    # ~400 ms warm), imported only when that transport is configured (S01.3).
    from src.collection.obd_collector import ObdSpec
    from src.collection.pids import REQUIRED_PIDS

    obd_pids = (
        config.collection.obd_pids
        if config.collection.obd_pids
        else tuple(spec.command.name for spec in REQUIRED_PIDS)
    )
    obd_spec = ObdSpec(
        port=config.hardware.elm327_port,
        pids=obd_pids,
        dids=config.collection.obd_dids,
        did_header=f"{config.collection.obd_did_request_id:X}",
    )

    async def obd(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_obd(obd_spec, queue, stop, reconnect_policy=reconnect_policy)

    return [can, obd, *aux_collectors]


async def collect_obd(
    spec: ObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    reconnect_policy: ReconnectPolicy | None,
) -> None:
    """:func:`src.collection.obd_collector.collect_obd`, imported on first call.

    Only the ELM327 transport reaches this, and it has already imported the
    module to build its :class:`ObdSpec`.
    """
    from src.collection.obd_collector import collect_obd as _collect_obd

    await _collect_obd(spec, queue, stop_event, reconnect_policy=reconnect_policy)


def _gps_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
) -> list[CollectorStarter]:
    """A GPS collector starter, or empty when no GPS is configured/enabled.

    Two backends emitting the same ``gps:*`` channels: ``serial`` reads NMEA
    from the receiver, ``gpsd`` reads gpsd's socket. They are mutually
    exclusive by construction — gpsd holds the serial port open, so only one
    process can have the receiver.
    """
    gps = config.hardware.gps
    if gps is None or not gps.enabled:
        return []

    if gps.source == "gpsd":
        gpsd_spec = GpsdSpec(
            host=gps.gpsd_host,
            port=gps.gpsd_port,
            read_timeout_seconds=gps.read_timeout_seconds,
        )

        async def gpsd_collector(
            queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event
        ) -> None:
            await collect_gpsd_from_config(
                gpsd_spec, queue, stop, reconnect_policy=reconnect_policy
            )

        return [gpsd_collector]

    spec = GpsSpec(
        device=gps.device,
        baudrate=gps.baudrate,
        read_timeout_seconds=gps.read_timeout_seconds,
    )

    async def gps_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_gps_from_config(spec, queue, stop, reconnect_policy=reconnect_policy)

    return [gps_collector]


def _sensor_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
) -> list[CollectorStarter]:
    """The add-on sensor CAN collector, or empty when it is not enabled."""
    sensor_can = config.hardware.sensor_can
    if not sensor_can.enabled:
        return []

    async def sensor_can_collector(
        queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event
    ) -> None:
        await collect_sensor_can_from_config(
            sensor_can, queue, stop, reconnect_policy=reconnect_policy
        )

    return [sensor_can_collector]


def _imu_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
    *,
    health: ImuHealth | None = None,
) -> list[CollectorStarter]:
    """The IMU collector, or empty when no IMU is configured.

    Only the LSM6DSOX (the CANary-HAT's IMU) has a collector; the other
    ``hardware.imu.source`` values predate the HAT and are refused here rather
    than silently recording nothing.
    """
    imu = config.hardware.imu
    if not imu.enabled:
        return []
    if imu.source != "lsm6dsox":
        raise ValueError(
            f"hardware.imu.source={imu.source!r} has no collector; "
            "the CANary-HAT's IMU is `lsm6dsox`"
        )

    async def imu_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_imu_from_config(
            imu, queue, stop, reconnect_policy=reconnect_policy, health=health
        )

    return [imu_collector]


def _supply_voltage_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
) -> list[CollectorStarter]:
    """The HAT's 12 V sense collector, or empty when it is not enabled."""
    supply = config.hardware.supply_voltage
    if not supply.enabled:
        return []

    async def supply_voltage_collector(
        queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event
    ) -> None:
        await collect_supply_voltage_from_config(
            supply, queue, stop, reconnect_policy=reconnect_policy
        )

    return [supply_voltage_collector]


def _usb_adc_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
) -> list[CollectorStarter]:
    """The USB ADC collector, or empty when it is not enabled.

    Refuses a channel the sensor CAN bus also produces: two instruments
    writing one ``sensor:<name>`` would interleave in a single column, and
    nothing downstream could tell which reading came from which.
    """
    usb_adc = config.hardware.usb_adc
    if not usb_adc.enabled:
        return []
    if config.hardware.sensor_can.enabled:
        analog = {c.name for c in load_analog_sensor_catalog(usb_adc.sensors).channels}
        can_names = {
            s.name for s in load_sensor_can_catalog(config.hardware.sensor_can.signals).signals
        }
        shared = sorted(analog & can_names)
        if shared:
            raise ValueError(
                f"sensor channels {shared} are produced by both hardware.usb_adc and "
                "hardware.sensor_can; disable one, or rename the channel in one catalog"
            )

    async def usb_adc_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_usb_adc_from_config(usb_adc, queue, stop, reconnect_policy=reconnect_policy)

    return [usb_adc_collector]


def _kline_collectors(config: CanaryConfig) -> list[CollectorStarter]:
    """The K-line collector, or empty when the HAT's K-line is not enabled.

    Enabled only means the hardware may be used; it transmits only while the
    active capture profile lists ``kline:`` channels.
    """
    kline = config.hardware.kline
    if not kline.enabled:
        return []
    from src.paths import DIDS_YAML

    collector = KlineCollector(kline, config.daemon.kline_profile, DIDS_YAML)

    async def kline_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collector.run(queue, stop)

    return [kline_collector]


__all__ = ["CollectorStarter", "default_collectors"]
