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
from src.collection.capture_profiles import CaptureProfileSources, CaptureProfileStore
from src.collection.gps_collector import GpsSpec, collect_gps_from_config
from src.collection.gps_gpsd import GpsdSpec, collect_gpsd_from_config
from src.collection.hat_adc_collector import Plan, collect_hat_adc_from_config
from src.collection.hat_revision import kline_pins, resolve_hat_revision
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
from src.collection.vehicle_power import VEHICLE_POWER
from src.config import CanaryConfig
from src.decode.analog_sensors import load_analog_sensor_catalog
from src.decode.hat_adc_board import load_hat_adc_board
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
        CaptureProfileSources.of(config.daemon),
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
        *_hat_adc_collectors(config, reconnect_policy),
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
                sleeps_with_bus=config.collection.obd_sleeps_with_bus,
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
    """Rev 2.0's 12 V sense (an ADS1015), or empty when it is not enabled.

    Rev 2.1 has no ADS1015: its 12 V sense is the ADS8688A's CH0, and
    :func:`_hat_adc_collectors` records it."""
    supply = config.hardware.supply_voltage
    if not supply.enabled:
        return []
    revision = resolve_hat_revision(config.hardware.hat_revision)
    if revision.supply_adc != "ads1015":
        return []
    # Before any collector starts, so the OBD collector knows to wait for
    # the first reading rather than transmit into an unknown port.
    VEHICLE_POWER.expect_readings()

    async def supply_voltage_collector(
        queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event
    ) -> None:
        await collect_supply_voltage_from_config(
            supply, queue, stop, reconnect_policy=reconnect_policy
        )

    return [supply_voltage_collector]


def _hat_adc_collectors(
    config: CanaryConfig,
    reconnect_policy: ReconnectPolicy,
) -> list[CollectorStarter]:
    """The rev 2.1 HAT's ADC collector, or empty when nothing asks for it.

    One collector owns the chip, for ``hardware.supply_voltage`` (the 12 V
    sense on CH0) and ``hardware.hat_adc`` (the sensor inputs) alike. Refuses
    a channel another instrument also produces: two writers of one
    ``sensor:<name>`` interleave in a single column (``FUEL_PRESSURE`` from
    both this chip and the USB ADC, say)."""
    hat_adc = config.hardware.hat_adc
    supply = config.hardware.supply_voltage
    if not (hat_adc.enabled or supply.enabled):
        return []
    revision = resolve_hat_revision(config.hardware.hat_revision)
    if revision.supply_adc != "ads8688a":
        if hat_adc.enabled:
            log.error(
                "hardware.hat_adc reads rev 2.1's ADS8688A; CANary-HAT rev %s has none, so "
                "its sensor inputs are not recorded",
                revision.name,
            )
        return []
    plan = Plan(supply=supply.enabled, sensors=hat_adc.enabled)
    ignored = sorted(
        supply.model_fields_set & {"i2c_bus", "i2c_address", "input_channel", "divider_ratio"}
    )
    if plan.supply and ignored:
        log.warning(
            "hardware.supply_voltage %s: rev 2.0's ADS1015 settings, ignored on CANary-HAT rev "
            "%s, whose 12 V sense is the ADS8688A's CH0 with its divider in %s",
            ", ".join(ignored),
            revision.name,
            hat_adc.board.name,
        )
    if plan.sensors:
        ours = plan.channels(load_hat_adc_board(hat_adc.board))
        others: dict[str, set[str]] = {}
        if config.hardware.usb_adc.enabled:
            others["hardware.usb_adc"] = {
                c.name for c in load_analog_sensor_catalog(config.hardware.usb_adc.sensors).channels
            }
        if config.hardware.sensor_can.enabled:
            others["hardware.sensor_can"] = {
                s.name for s in load_sensor_can_catalog(config.hardware.sensor_can.signals).signals
            }
        for owner, names in others.items():
            shared = sorted(ours & names)
            if shared:
                raise ValueError(
                    f"sensor channels {shared} are produced by both hardware.hat_adc and "
                    f"{owner}; disable one, or rename the channel in one map"
                )
    if plan.supply:
        # Before any collector starts, as for rev 2.0's sense (above).
        VEHICLE_POWER.expect_readings()

    async def hat_adc_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_hat_adc_from_config(
            hat_adc, supply, plan, revision, queue, stop, reconnect_policy=reconnect_policy
        )

    return [hat_adc_collector]


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

    pins = kline_pins(kline, resolve_hat_revision(config.hardware.hat_revision))
    log.info("K-line on %s: EN GPIO%d, pull-up GPIO%d", pins.tty, pins.enable, pins.pullup)
    collector = KlineCollector(kline, config.daemon.kline_profile, DIDS_YAML, pins=pins)

    async def kline_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collector.run(queue, stop)

    return [kline_collector]


__all__ = ["CollectorStarter", "default_collectors"]
