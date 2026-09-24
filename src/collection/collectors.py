"""Default collector-starter set for the live daemon (Task 4.7, refactoring.md).

``CollectorStarter`` was defined twice (``src/main.py`` and
``src/collection/replay.py``) — one canonical alias here, both re-export it.

``default_collectors``/``_gps_collectors`` (config → concrete collector
coroutines for CAN/OBD/GPS) used to live in ``src/main.py``, with
``src/daemon/runtime.py`` reaching back into it via ``from src.main import
_default_collectors`` — a daemon → main import inversion (``main.py`` is the
CLI entry point; it should depend on the daemon package, not the other way
around). This module is their new home, alongside the collector
constructors they call; ``main.py`` re-exports ``_default_collectors`` for
its own ``run_collection`` and existing callers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.analysis.tune_revisions import load_tune_revisions
from src.collection import tuning_profile
from src.collection.calibration_id import resolve_calibration_id_spec
from src.collection.can_collector import ReceiveStatsCallback, collect_can_from_config
from src.collection.can_obd_collector import (
    CanObdSpec,
    ObdSchedulerMetrics,
    collect_can_obd_from_config,
    resolve_did_session_spec,
)
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.custom_pids import load_custom_pids
from src.collection.discovery import vehicle_key
from src.collection.gps_collector import GpsSpec, collect_gps_from_config
from src.collection.gps_gpsd import GpsdSpec, collect_gpsd_from_config
from src.collection.imu_collector import collect_imu_from_config
from src.collection.obd_scheduler import (
    ForcePollTrigger,
    FreezeFrameTrigger,
    did_access,
    did_response_widths,
    known_did_names,
    memory_channels,
    parse_did_token,
)
from src.collection.queueing import CollectorHandoffStats
from src.collection.reconnect import ReconnectPolicy
from src.collection.sensor_can_collector import collect_sensor_can_from_config
from src.config import CanaryConfig
from src.control.calibration import active_registry_path
from src.telemetry import TelemetryRecord

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
) -> list[CollectorStarter]:
    """Config -> collector starters.

    ``can_receive_stats_callback`` (C07.5) receives the passive CAN receive
    path's cumulative counters — used by the two branches that run the
    standalone sniffer (OBD ``disabled``, and legacy ELM327 beside passive
    CAN); the ``canable`` branch's combined collector has its own loop.
    """
    capture_store = CaptureProfileStore(
        config.daemon.capture_profiles,
        config.collection,
        config.daemon.behavior_models,
    )
    capture_catalog = capture_store.load()
    default_capture = capture_catalog.by_name("default")
    assert default_capture is not None
    # Runtime state is intentionally reset on each daemon start (capture
    # profiles are persistent; the *active capture* is not).
    tuning_profile.configure_default(default_capture)
    inventory_dids = known_did_names()
    tuning_profile.configure_did_aliases(
        {
            token: f"DID_{parse_did_token(token):04X}"
            for token in dict.fromkeys(
                [
                    *config.collection.obd_dids,
                    *inventory_dids,
                    *(did for profile in capture_catalog.profiles for did in profile.mode22),
                ]
            )
        }
    )
    did_widths, did_width_sources = did_response_widths()
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
        *_imu_collectors(config, reconnect_policy),
    ]

    if config.collection.obd_source == "canable":
        # Task 2.6: loaded once here, not per poll tick. Absent file ⇒ an
        # empty set (load_custom_pids' own graceful-missing-file behavior).
        custom_pids = load_custom_pids(config.daemon.custom_pids).custom_pids
        configured_pids = config.collection.obd_pids
        request_pids = (
            configured_pids
            if any(token.strip().lower() == "auto" for token in configured_pids)
            else tuple(
                dict.fromkeys(
                    [
                        *configured_pids,
                        *(
                            channel
                            for profile in capture_catalog.profiles
                            for channel in profile.mode01
                        ),
                    ]
                )
            )
        )
        request_dids = tuple(
            dict.fromkeys(
                [
                    *config.collection.obd_dids,
                    *inventory_dids,
                    *(did for profile in capture_catalog.profiles for did in profile.mode22),
                ]
            )
        )
        # RAM addresses belong to the ECU's program code, which the flashes
        # made on this vehicle do not change (operator, 2026-09-21: a
        # 2026-09-20 table-edit flash left every address in place, confirmed
        # by tools.verify_memory_channels). So channels are not gated on the
        # running tune revision; the known-answer verify is the evidence check
        # for a moved address, run on demand.
        selected_memory = tuple(
            dict.fromkeys(name for profile in capture_catalog.profiles for name in profile.memory)
        )
        usable_memory = [c for c in memory_channels() if c.name in selected_memory]
        # The operational registry (a flash recorded through the daemon, W06.5)
        # wins over the tracked profile copy, same as /control/calibration.
        registry = load_tune_revisions(active_registry_path(config))
        current = None if registry is None else registry.at(datetime.now(UTC))
        tuning_profile.configure_running_tune_revision(
            None if current is None else current.revision,
            calibration_id=None if current is None else current.calibration_id,
        )
        can_obd_spec = CanObdSpec(
            can=config.hardware.can,
            poll_rate_hz=config.collection.obd_poll_rate_hz,
            # ``auto`` already discovers every supported Mode-01 channel;
            # explicit plans and DIDs are expanded to the saved-profile union.
            # The runtime gate keeps default polling identical while making a
            # profile switch restart-free.
            pids=request_pids,
            dids=request_dids,
            capture_profiles_enabled=True,
            batch_max_pids=config.collection.obd_batch_max_pids,
            did_batch_max_dids=config.collection.obd_did_batch_max_dids,
            did_response_widths=did_widths,
            did_response_width_sources=did_width_sources,
            did_request_id=config.collection.obd_did_request_id,
            request_timeout_seconds=config.collection.obd_request_timeout_seconds,
            did_session=resolve_did_session_spec(config.collection),
            dtc_poll_interval_seconds=config.collection.obd_dtc_poll_interval_seconds,
            service_poll_interval_seconds=config.collection.obd_monitor_poll_interval_seconds,
            service_mids=config.collection.obd_monitor_mids,
            freeze_frame_trigger=freeze_frame_trigger,
            force_poll_trigger=force_poll_trigger,
            custom_pids=tuple(custom_pids),
            memory=tuple(usable_memory),
            priority_channels=config.collection.obd_priority_channels,
            discovery_budget_requests_per_second=(
                config.collection.obd_discovery_budget_requests_per_second
            ),
            # Support maps cached per vehicle across reconnects (C06.6).
            discovery_key=vehicle_key(
                config.vehicle.vin,
                config.vehicle.make,
                config.vehicle.model,
                config.vehicle.year,
            ),
            # W02.2: the ECU's own calibration id, read per the vehicle's
            # dids.yaml access block (which session it needs is data).
            calibration_id=resolve_calibration_id_spec(did_access()),
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
        await collect_imu_from_config(imu, queue, stop, reconnect_policy=reconnect_policy)

    return [imu_collector]


__all__ = ["CollectorStarter", "default_collectors"]
