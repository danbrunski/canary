"""Daemon assembly — wires producers, sinks, dashboard, and model managers.

The pieces all exist; this module is the *connection diagram*:

* Collectors (CAN, OBD) push ``TelemetryRecord`` onto an asyncio queue.
* ``_drain_queue`` pops each record, writes it to Parquet, *and publishes
  it onto the telemetry Broadcaster* so ``/ws/telemetry`` subscribers see
  it live.
* ``ModelManager`` instances poll the models directory; ``/status`` reports
  whatever's currently loaded.
* ``AnomalyEventEmitter`` is constructed up front and wired to the
  anomalies ``Broadcaster`` so events emitted (Milestone B: when live
  inference is wired) fan out to ``/ws/anomalies`` and the
  ``AnomalyEventLogger``'s JSONL at the same time.
* The FastAPI app runs in the same event loop via ``uvicorn.Server``.

Live inference (the row stream → IF/LSTM observe path) is NOT wired here
yet — it's Milestone B and needs a live preprocessing pipeline
(incremental decode → resample → segment → rolling features → scaler).
This module sets up everything around that gap so the loop is otherwise
complete; the day Milestone B lands, the only new wiring is an inference
task added next to the existing collectors.

Why a separate module from ``src.main``: ``main.py`` is the CLI / signal-
handling shell; this module is the assembly that's directly importable
from tests without going through ``asyncio.run`` or a SIGTERM dance.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.collection.queueing import QueueDepthMonitor
from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.dashboard.broadcast import Broadcaster
from src.models.manager import ModelManager, manager_for_artifact
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Assembled state
# ---------------------------------------------------------------------------


@dataclass
class DaemonComponents:
    """Long-lived objects the daemon stitches together.

    Held in one record so a test can construct + inspect them without
    starting any background tasks. The real `run_daemon` builds one of
    these, then starts the asyncio tasks that drive them.
    """

    config: CanaryConfig
    telemetry_broadcaster: Broadcaster
    anomalies_broadcaster: Broadcaster
    event_logger: AnomalyEventLogger
    emitter: AnomalyEventEmitter
    model_managers: dict[str, ModelManager[Any]]


def build_components(config: CanaryConfig) -> DaemonComponents:
    """Construct all long-lived components from config. No I/O beyond mkdir."""
    telemetry_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    anomalies_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)

    event_logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    emitter = _make_broadcasting_emitter(event_logger, anomalies_bus)
    model_managers = _load_model_managers(config)

    return DaemonComponents(
        config=config,
        telemetry_broadcaster=telemetry_bus,
        anomalies_broadcaster=anomalies_bus,
        event_logger=event_logger,
        emitter=emitter,
        model_managers=model_managers,
    )


def _make_broadcasting_emitter(
    logger: AnomalyEventLogger, anomalies_bus: Broadcaster
) -> AnomalyEventEmitter:
    """Wrap ``AnomalyEventLogger`` so every persisted event is also published
    on the broadcaster — keeps the logger-as-source-of-truth + the live-push
    paths in lockstep through one call site."""

    class _BroadcastingLogger:
        """Stand-in that mirrors AnomalyEventLogger's surface but also publishes."""

        def __init__(self, inner: AnomalyEventLogger, bus: Broadcaster) -> None:
            self._inner = inner
            self._bus = bus

        def log(self, event: AnomalyEvent) -> Path:
            path = self._inner.log(event)
            self._bus.publish(event.to_dict())
            return path

        def close(self) -> None:
            self._inner.close()

    # AnomalyEventEmitter just needs `.log(event)`; duck-type satisfies it.
    return AnomalyEventEmitter(_BroadcastingLogger(logger, anomalies_bus))  # type: ignore[arg-type]


def _load_model_managers(config: CanaryConfig) -> dict[str, ModelManager[Any]]:
    """Build one ``ModelManager`` per detector, loading the initial artifact
    if present. Missing artifacts are fine — the manager keeps polling, and
    ``current`` is ``None`` until something lands."""
    managers: dict[str, ModelManager[Any]] = {}
    if_path = config.daemon.models_dir / config.daemon.isolation_forest_artifact
    lstm_path = config.daemon.models_dir / config.daemon.lstm_artifact

    if config.models.isolation_forest.enabled:
        from src.models.isolation_forest import load_isolation_forest_model

        managers["isolation_forest"] = manager_for_artifact(
            if_path, load_isolation_forest_model, model_type="isolation_forest"
        )
    if config.models.lstm_autoencoder.enabled:
        try:
            from src.models.lstm_training import load_lstm_autoencoder_model
        except ImportError:
            log.warning("lstm_autoencoder enabled but torch is unavailable; skipping ModelManager")
        else:
            managers["lstm_autoencoder"] = manager_for_artifact(
                lstm_path, load_lstm_autoencoder_model, model_type="lstm_autoencoder"
            )
    return managers


# ---------------------------------------------------------------------------
# Queue draining with broadcaster fan-out
# ---------------------------------------------------------------------------


async def drain_queue_with_broadcast(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    telemetry_broadcaster: Broadcaster | None = None,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
) -> None:
    """Drain telemetry into storage + the live telemetry broadcaster.

    Extension of ``src.main._drain_queue``: same Parquet write path, same
    queue-depth / disk-space monitoring, but each record is *also*
    published onto ``telemetry_broadcaster`` so ``/ws/telemetry``
    subscribers see it without polling Parquet. Publishing is best-effort
    — a full subscriber queue drops the oldest message, never blocks the
    writer.
    """
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
            if telemetry_broadcaster is not None:
                telemetry_broadcaster.publish(_record_to_dict(record))
        finally:
            queue.task_done()
            queue_monitor.observe(queue)

    writer.close()
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)


def _record_to_dict(record: TelemetryRecord) -> dict[str, Any]:
    """Live-stream shape: same {timestamp, source, channel, value, unit}
    the REST ``/telemetry/latest`` endpoint returns, so the dashboard
    can render the two paths identically."""
    return {
        "timestamp": record.timestamp,
        "source": record.source,
        "channel": record.channel,
        "value": record.value,
        "unit": record.unit,
    }


# ---------------------------------------------------------------------------
# Background tasks the daemon runs alongside the collectors
# ---------------------------------------------------------------------------


async def poll_model_managers(
    managers: dict[str, ModelManager[Any]],
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """Periodically call ``manager.poll()`` on each ModelManager.

    Light task: the manager's mtime check makes each tick cheap when nothing
    changed. Cadence comes from ``daemon.model_poll_seconds`` — refits are
    minute-to-hour scale so 30s default is fine.
    """
    while not stop_event.is_set():
        for name, manager in managers.items():
            try:
                attempt = manager.poll()
            except Exception:  # noqa: BLE001 — manager.poll already swallows + logs internally
                log.exception("model manager poll raised", extra={"manager": name})
                continue
            if attempt.kind == "swapped":
                log.info(
                    "model swapped",
                    extra={"manager": name, "version": attempt.new_version},
                )
            elif attempt.kind == "rejected":
                log.warning(
                    "swap rejected",
                    extra={"manager": name, "reason": attempt.detail},
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


@asynccontextmanager
async def serve_in_background(
    components: DaemonComponents,
    *,
    server_factory: Callable[..., uvicorn.Server] | None = None,
) -> AsyncIterator[uvicorn.Server]:
    """Run the FastAPI dashboard inside the same event loop as the collectors.

    The returned context manager runs the uvicorn server in a background
    task; on exit, sends the graceful-shutdown signal and waits for the
    task. ``server_factory`` is injectable for tests — pass a factory
    that builds a server bound to a high local port, e.g.
    ``lambda **kw: uvicorn.Server(uvicorn.Config(**kw, port=0))``.

    Why uvicorn.Server.serve() rather than uvicorn.run(): the latter spins
    its own event loop, which conflicts with the daemon's. ``Server.serve``
    is the documented "I already have a loop" entry.
    """
    app = create_app(
        components.config,
        anomalies_dir=components.config.daemon.anomalies_dir,
        models_dir=components.config.daemon.models_dir,
        telemetry_broadcaster=components.telemetry_broadcaster,
        anomalies_broadcaster=components.anomalies_broadcaster,
    )
    if server_factory is None:
        server_factory = uvicorn.Server
    server_config = uvicorn.Config(
        app,
        host=components.config.dashboard.host,
        port=components.config.dashboard.port,
        log_level="info",
        lifespan="on",
    )
    server = server_factory(config=server_config)
    serve_task = asyncio.create_task(server.serve())
    # uvicorn flips server.started True once the listening socket is open.
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    try:
        yield server
    finally:
        server.should_exit = True
        await serve_task


# ---------------------------------------------------------------------------
# Top-level run loop (extension of src.main.run; not yet wired from main)
# ---------------------------------------------------------------------------


async def run_daemon(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | None = None,
    components: DaemonComponents | None = None,
) -> DaemonComponents:
    """Run the full daemon until ``stop_event`` fires.

    Builds components (or accepts pre-built ones for tests), spins up:
      * the collectors (CAN, OBD) → ``asyncio.Queue``
      * ``drain_queue_with_broadcast`` → Parquet + telemetry broadcaster
      * ``poll_model_managers`` → hot-swap loop
      * the dashboard via ``serve_in_background``

    Returns the components so a caller can inspect counters / model
    versions for diagnostics or tests.
    """
    components = components or build_components(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=config.collection.queue_max_size)
    storage_writer = (
        ParquetTelemetryWriter(config.collection.storage_path) if writer is None else writer
    )

    # Lazy import — keeps daemon module importable without the collector deps
    # at module-load time.
    from src.main import _default_collectors

    active_collectors = _default_collectors(config) if collectors is None else list(collectors)

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            storage_writer,
            stop_event,
            telemetry_broadcaster=components.telemetry_broadcaster,
            storage_path=config.collection.storage_path,
            storage_warning_mb=config.collection.storage_warning_mb,
            queue_warning_fraction=config.collection.queue_warning_fraction,
        )
    )
    poll_task = asyncio.create_task(
        poll_model_managers(
            components.model_managers,
            stop_event,
            interval_seconds=config.daemon.model_poll_seconds,
        )
    )
    collector_tasks = [
        asyncio.create_task(starter(queue, stop_event)) for starter in active_collectors
    ]
    stop_task = asyncio.create_task(stop_event.wait())

    async with serve_in_background(components):
        try:
            done, _ = await asyncio.wait(
                [stop_task, *collector_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop_task:
                    task.result()
        finally:
            stop_event.set()
            await asyncio.gather(*collector_tasks, return_exceptions=True)
            await queue.join()
            await drain_task
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            components.event_logger.close()
    return components


__all__ = [
    "DaemonComponents",
    "build_components",
    "drain_queue_with_broadcast",
    "poll_model_managers",
    "run_daemon",
    "serve_in_background",
]
