"""Entry point for the CANary daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from src.collection.can_collector import collect_can_from_config
from src.collection.obd_collector import ObdSpec, collect_obd
from src.collection.pids import REQUIRED_PIDS
from src.collection.queueing import QueueDepthMonitor
from src.collection.reconnect import ReconnectPolicy
from src.config import CanaryConfig
from src.log import configure_logging
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter, delete_expired_sessions
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

CollectorStarter = Callable[
    [asyncio.Queue[TelemetryRecord], asyncio.Event],
    Coroutine[Any, Any, None],
]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canary",
        description="CANary on-vehicle anomaly-detection daemon.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml configuration file (default: ./canary.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


async def _drain_queue(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
) -> None:
    """Drain telemetry records into storage until stopped and queue is empty."""
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)

    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            if writer.flush_due():
                writer.flush()
                if storage_path is not None and storage_warning_mb is not None:
                    warn_if_storage_low(storage_path, storage_warning_mb)
            continue

        try:
            writer.write(record)
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
    writer.close()
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)


def _default_collectors(config: CanaryConfig) -> list[CollectorStarter]:
    obd_pids = tuple(spec.command.name for spec in REQUIRED_PIDS)
    obd_spec = ObdSpec(port=config.hardware.elm327_port, pids=obd_pids)
    reconnect_policy = ReconnectPolicy(
        initial_delay_seconds=config.collection.reconnect_initial_delay_seconds,
        max_delay_seconds=config.collection.reconnect_max_delay_seconds,
    )

    async def can(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_can_from_config(
            config.hardware.can,
            queue,
            stop,
            reconnect_policy=reconnect_policy,
        )

    async def obd(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await collect_obd(obd_spec, queue, stop, reconnect_policy=reconnect_policy)

    return [can, obd]


async def run_collection(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[CollectorStarter] | None = None,
    writer: ParquetTelemetryWriter | None = None,
) -> None:
    """Run collectors and persist telemetry until stop_event fires."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=config.collection.queue_max_size)
    storage_writer = (
        ParquetTelemetryWriter(config.collection.storage_path) if writer is None else writer
    )
    active_collectors = _default_collectors(config) if collectors is None else list(collectors)

    writer_task = asyncio.create_task(
        _drain_queue(
            queue,
            storage_writer,
            stop_event,
            storage_path=config.collection.storage_path,
            storage_warning_mb=config.collection.storage_warning_mb,
            queue_warning_fraction=config.collection.queue_warning_fraction,
        )
    )
    collector_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(starter(queue, stop_event)) for starter in active_collectors
    ]
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        done, _pending = await asyncio.wait(
            [stop_task, *collector_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is not stop_task:
                task.result()
    finally:
        stop_event.set()
        await asyncio.gather(*collector_tasks, return_exceptions=True)
        await queue.join()
        await writer_task
        deleted = delete_expired_sessions(
            config.collection.storage_path,
            config.collection.session_retention_days,
        )
        if deleted:
            log.info("expired sessions deleted", extra={"count": len(deleted)})


async def run(
    config: CanaryConfig,
    stop_event: asyncio.Event | None = None,
    *,
    start_collectors: bool = True,
) -> None:
    """Start the daemon and block until the stop event fires.

    If `stop_event` is None, one is created and wired to SIGTERM/SIGINT. Tests
    pass an explicit event so they can trigger shutdown without signals.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    log.info(
        "canary starting",
        extra={
            "vehicle": f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}",
            "obd_poll_rate_hz": config.collection.obd_poll_rate_hz,
            "storage_path": str(config.collection.storage_path),
        },
    )

    if start_collectors:
        await run_collection(config, stop_event)
    else:
        await stop_event.wait()
    log.info("canary stopping")


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
