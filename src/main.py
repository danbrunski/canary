"""Entry point for the CANary daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from pathlib import Path

from src.collection.collectors import CollectorStarter
from src.collection.collectors import default_collectors as _default_collectors
from src.collection.queueing import QueueDepthMonitor
from src.config import CanaryConfig
from src.log import configure_logging
from src.profiles import resolve_active_profile, scope_config
from src.startup import StartupTimeline
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter, delete_expired_sessions
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


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
        "--collection-only",
        action="store_true",
        help=(
            "Run only the collection loop (no dashboard, no model managers, "
            "no broadcasters). Useful when running `record.py`-style headless "
            "captures or when the dashboard port is occupied."
        ),
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Force a vehicle profile slug (skips the startup VIN probe). Use for "
            "sim/replay or to target a specific vehicle's models. Default: resolve "
            "the profile by reading the VIN at startup."
        ),
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
            protect={storage_writer.paths.root},
        )
        if deleted:
            log.info("expired sessions deleted", extra={"count": len(deleted)})


async def run(
    config: CanaryConfig,
    stop_event: asyncio.Event | None = None,
    *,
    start_collectors: bool = True,
    full_daemon: bool = True,
    startup: StartupTimeline | None = None,
) -> None:
    """Start the daemon and block until the stop event fires.

    If ``stop_event`` is None, one is created and wired to SIGTERM/SIGINT. Tests
    pass an explicit event so they can trigger shutdown without signals.

    ``full_daemon=True`` (the default) runs the assembled daemon —
    collectors + writer + telemetry broadcaster + model manager polling +
    dashboard server. ``full_daemon=False`` falls back to the bare
    collection loop (``run_collection``), kept for the test paths that
    don't want the dashboard port bound.

    ``startup`` (S01.1) is the timeline :func:`main` began at process start;
    the full daemon serves it as ``/status``'s ``startup`` block.
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
            "full_daemon": full_daemon,
        },
    )
    if startup is not None:
        startup.mark("canary_starting")

    if not start_collectors:
        await stop_event.wait()
    elif full_daemon:
        # Lazy import keeps `src.main` importable in environments missing
        # the optional dashboard deps (uvicorn). The collection-only path
        # still works.
        from src.daemon import run_daemon

        if startup is not None:
            startup.mark("daemon_imported")
        await run_daemon(config, stop_event, startup=startup)
    else:
        await run_collection(config, stop_event)
    log.info("canary stopping")


def main(argv: Sequence[str] | None = None) -> None:
    # S01.1: measured from the process start (/proc/self/stat), so the
    # interpreter and this module's imports are inside the first stage.
    startup = StartupTimeline()
    startup.mark("main_entered")
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    if config.profiles.enabled:
        # Identify the vehicle (by VIN) and scope every vehicle-specific path
        # under its profile slug, so two vehicles never share a corpus/models.
        profile = resolve_active_profile(config, override_slug=args.profile)
        config = scope_config(config, profile)
        log.info(
            "active vehicle profile",
            extra={
                "slug": profile.slug,
                "profile_name": profile.name or None,
                "vin": profile.vin or None,
            },
        )
    startup.mark("profile_resolved")
    asyncio.run(run(config, full_daemon=not args.collection_only, startup=startup))


if __name__ == "__main__":
    main()
