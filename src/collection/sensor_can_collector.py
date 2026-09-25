"""Listen to add-on sensors on a second, private CAN interface.

Receive-only: this module never calls ``bus.send()``. The vehicle bus stays
``can0`` and its collectors are untouched; this bus carries only hardware the
operator added (first: an AEM X-Series fuel-pressure gauge), so none of the
vehicle bus's budget or MCP2515 limits apply to it.

Frames are filtered to the catalogued arbitration IDs in the driver and decoded
through ``src.decode.sensor_can``; each reading becomes a ``sensor:<NAME>``
record stamped with the frame's receive time, so it pairs same-instant with
the OBD path like any other source. The raw payload rides along so a layout
correction can be re-decoded from stored sessions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import can

from src.collection.can_bus import bus_kwargs, receive_frame
from src.collection.frame_time import frame_arrival
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import DEFAULT_RECONNECT_POLICY, ReconnectPolicy, run_with_reconnect
from src.config import SensorCanConfig
from src.decode.sensor_can import SensorCanCatalog, decode_frame, load_sensor_can_catalog
from src.telemetry import SENSOR_SOURCE, TelemetryRecord

log = logging.getLogger(__name__)

#: How long one blocking receive may wait before re-checking the stop event.
RECV_TIMEOUT_SECONDS = 0.5
#: Parse-error log throttle identity for the sensor bus.
_PARSE_ERROR_OWNER = "sensor-can"


@dataclass
class SensorCanStats:
    """Exact receive counters for the sensor bus; the log line is throttled."""

    frames_seen: int = 0
    #: Malformed frames the driver could not parse (slcan noise), dropped.
    parse_errors: int = 0

    def count_parse_error(self, _exc: Exception) -> None:
        self.parse_errors += 1


def sensor_bus_factory(config: SensorCanConfig, catalog: SensorCanCatalog) -> can.BusABC:
    """Open the sensor bus with a driver-level filter to the catalogued frames."""
    filters: list[Any] = [
        {
            "can_id": signal.arbitration_id,
            "can_mask": 0x1FFFFFFF if signal.extended else 0x7FF,
            "extended": signal.extended,
        }
        for signal in catalog.signals
    ]
    return can.Bus(
        **bus_kwargs(config.interface, config.channel, config.bitrate), can_filters=filters
    )


def frame_to_records(catalog: SensorCanCatalog, msg: can.Message) -> list[TelemetryRecord]:
    # The receive paths' one stamp policy (R02.4), so a sensor reading pairs
    # same-instant with the vehicle bus even after a stale driver stamp.
    stamp = frame_arrival(msg).at
    data = bytes(msg.data)
    return [
        TelemetryRecord.from_epoch(
            stamp,
            source=SENSOR_SOURCE,
            channel=reading.name,
            value=reading.value,
            unit=reading.unit,
            payload=data,
            metadata={"arbitration_id": f"{msg.arbitration_id:#010x}"},
        )
        for reading in decode_frame(catalog, msg.arbitration_id, msg.is_extended_id, data)
    ]


async def collect_sensor_can(
    bus: can.BusABC,
    catalog: SensorCanCatalog,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    drop_counter: QueueDropCounter | None = None,
    stats: SensorCanStats | None = None,
) -> None:
    on_drop = QueueDropCounter("sensor") if drop_counter is None else drop_counter
    counters = SensorCanStats() if stats is None else stats
    while not stop_event.is_set():
        # ``receive_frame`` turns an slcan parse error into a counted, dropped
        # frame; a bare ``bus.recv`` let it end the collector (R02.1).
        msg = await asyncio.to_thread(
            receive_frame,
            bus,
            timeout=RECV_TIMEOUT_SECONDS,
            on_frame=None,
            on_parse_error=counters.count_parse_error,
            parse_error_owner=_PARSE_ERROR_OWNER,
        )
        if msg is None:
            continue
        counters.frames_seen += 1
        if msg.is_error_frame or msg.is_remote_frame:
            continue
        for record in frame_to_records(catalog, msg):
            put_nowait_or_drop(queue, record, on_drop=on_drop)


async def collect_sensor_can_from_config(
    config: SensorCanConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[SensorCanConfig, SensorCanCatalog], can.BusABC] = sensor_bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
) -> None:
    """Own the configured sensor bus for the daemon queue-producer path.

    A missing or invalid catalog raises immediately (``ValueError`` is not
    retried): a sensor bus with no layouts would record nothing, silently.
    """
    catalog = load_sensor_can_catalog(config.signals)
    unverified = [s.name for s in catalog.signals if not s.verified]
    if unverified:
        log.warning(
            "sensor CAN layouts not yet checked against the device: %s — values are "
            "recorded but the decode is unconfirmed (candump beside the gauge face)",
            ", ".join(unverified),
            extra={"unverified_signals": unverified},
        )
    drop_counter = QueueDropCounter("sensor")

    async def _attempt(capture: Callable[[can.BusABC], None]) -> None:
        bus = factory(config, catalog)
        capture(bus)
        log.info(
            "sensor CAN collection started",
            extra={
                "channel": config.channel,
                "bitrate": config.bitrate,
                "signals": [s.name for s in catalog.signals],
            },
        )
        await collect_sensor_can(bus, catalog, queue, stop_event, drop_counter=drop_counter)

    def _teardown(bus: can.BusABC) -> None:
        try:
            bus.shutdown()
        except Exception:  # noqa: BLE001 — best-effort close on a possibly-dead handle
            pass

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_teardown,
        log=log,
        reconnect_message="sensor CAN collector failed; reconnecting",
        log_extra=lambda: {"channel": config.channel},
    )


__all__ = [
    "SensorCanStats",
    "collect_sensor_can",
    "collect_sensor_can_from_config",
    "frame_to_records",
    "sensor_bus_factory",
]
