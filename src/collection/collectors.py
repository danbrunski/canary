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
from collections.abc import Callable, Coroutine
from typing import Any

from src.collection.can_collector import collect_can_from_config
from src.collection.can_obd_collector import (
    CanObdSpec,
    ObdSchedulerMetrics,
    collect_can_obd_from_config,
    resolve_did_session_spec,
)
from src.collection.custom_pids import load_custom_pids
from src.collection.gps_collector import GpsSpec, collect_gps_from_config
from src.collection.gps_gpsd import GpsdSpec, collect_gpsd_from_config
from src.collection.obd_collector import ObdSpec, collect_obd
from src.collection.obd_scheduler import FreezeFrameTrigger
from src.collection.pids import REQUIRED_PIDS
from src.collection.reconnect import ReconnectPolicy
from src.config import CanaryConfig
from src.telemetry import TelemetryRecord

CollectorStarter = Callable[
    ["asyncio.Queue[TelemetryRecord]", asyncio.Event], Coroutine[Any, Any, None]
]


def default_collectors(
    config: CanaryConfig,
    *,
    obd_scheduler_metrics_callback: Callable[[ObdSchedulerMetrics], None] | None = None,
    freeze_frame_trigger: FreezeFrameTrigger | None = None,
) -> list[CollectorStarter]:
    reconnect_policy = ReconnectPolicy(
        initial_delay_seconds=config.collection.reconnect_initial_delay_seconds,
        max_delay_seconds=config.collection.reconnect_max_delay_seconds,
    )
    # GPS runs alongside whatever OBD/CAN source is configured (it's an
    # independent serial device), so it's appended to every branch's collector
    # list rather than gated on obd_source.
    gps_collectors = _gps_collectors(config, reconnect_policy)

    if config.collection.obd_source == "canable":
        # Task 2.6: loaded once here, not per poll tick. Absent file ⇒ an
        # empty set (load_custom_pids' own graceful-missing-file behavior).
        custom_pids = load_custom_pids(config.daemon.custom_pids).custom_pids
        can_obd_spec = CanObdSpec(
            can=config.hardware.can,
            poll_rate_hz=config.collection.obd_poll_rate_hz,
            pids=config.collection.obd_pids,
            dids=config.collection.obd_dids,
            did_request_id=config.collection.obd_did_request_id,
            request_timeout_seconds=config.collection.obd_request_timeout_seconds,
            did_session=resolve_did_session_spec(config.collection),
            dtc_poll_interval_seconds=config.collection.obd_dtc_poll_interval_seconds,
            service_poll_interval_seconds=config.collection.obd_monitor_poll_interval_seconds,
            freeze_frame_trigger=freeze_frame_trigger,
            custom_pids=tuple(custom_pids),
            priority_channels=config.collection.obd_priority_channels,
        )

        async def canable_obd(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
            await collect_can_obd_from_config(
                can_obd_spec,
                queue,
                stop,
                reconnect_policy=reconnect_policy,
                on_scheduler_metrics=obd_scheduler_metrics_callback,
            )

        return [canable_obd, *gps_collectors]

    async def can(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_can_from_config(
            config.hardware.can,
            queue,
            stop,
            reconnect_policy=reconnect_policy,
        )

    if config.collection.obd_source == "disabled":
        return [can, *gps_collectors]

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

    return [can, obd, *gps_collectors]


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


__all__ = ["CollectorStarter", "default_collectors"]
