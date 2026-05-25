from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from src.config import CanaryConfig
from src.control.capture import CaptureController
from src.control.replay_control import ReplayController
from src.decode.behavior_models import DEFAULT_BEHAVIOR_MODELS_PATH
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

from .components import DaemonComponents, build_components
from .drain import drain_queue_with_broadcast
from .inference import build_live_feature_source, run_live_inference
from .polling import poll_behavior_models, poll_config, poll_model_managers
from .retention import _active_replay_dir, run_retention_sweeper
from .server import serve_in_background

log = logging.getLogger(__name__)

DRAIN_SHUTDOWN_SECONDS = 5.0

__all__ = [
    "DRAIN_SHUTDOWN_SECONDS",
    "run_daemon",
    "run_retention_sweeper",
    "serve_in_background",
]


async def run_daemon(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | None = None,
    components: DaemonComponents | None = None,
    enable_live_inference: bool = True,
) -> DaemonComponents:
    """Run the full daemon until ``stop_event`` fires.

    Builds components (or accepts pre-built ones for tests), spins up:
      * the collectors (CAN, OBD) → ``asyncio.Queue``
      * ``drain_queue_with_broadcast`` → Parquet + telemetry broadcaster +
        optional live source (every drained record fans out to all three)
      * ``run_live_inference`` → per-tick IF + LSTM + behavior scoring,
        fused into one calibrated ``fused`` event alongside the per-source
        events → emitter → anomalies broadcaster + JSONL (when
        ``enable_live_inference``; degrades to behavior/fusion-only when no
        model artifact is loaded)
      * ``poll_model_managers`` → hot-swap loop
      * ``run_retention_sweeper`` → age + byte-cap + low-space retention
      * the dashboard via ``serve_in_background``

    Returns the components so a caller can inspect counters / model
    versions for diagnostics or tests.
    """
    components = components or build_components(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=config.collection.queue_max_size)
    storage_writer = (
        ParquetTelemetryWriter(config.collection.storage_path) if writer is None else writer
    )
    components.capture_controller = CaptureController(lambda: storage_writer.paths.root)
    components.replay_controller = ReplayController(
        queue=queue,
        writer=storage_writer,
        anomaly_logger=components.event_logger,
        storage_root=config.collection.storage_path,
    )

    # Build the live source (no-op if bindings/behavior_models are absent on
    # disk — observe still works, just without decoded:* / deviation:* columns).
    live_source = build_live_feature_source(components) if enable_live_inference else None

    # Lazy import — keeps daemon module importable without the collector deps
    # at module-load time.
    from src.main import _default_collectors

    if collectors is not None:
        active_collectors = list(collectors)
    elif config.collection.replay_session:
        # Simulation backend: replay a recorded session instead of opening
        # real CAN/OBD. Same queue → same decode/broadcast/inference path.
        from src.collection.replay import make_replay_collector

        active_collectors = [make_replay_collector(config)]
        log.info(
            "replay mode: real collectors disabled",
            extra={"session": config.collection.replay_session},
        )
    else:
        active_collectors = _default_collectors(
            config,
            obd_scheduler_metrics_callback=components.health_monitor.observe_obd_scheduler,
        )

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            storage_writer,
            stop_event,
            telemetry_broadcaster=components.telemetry_broadcaster,
            live_source=live_source,
            gps_tracker=components.gps_tracker,
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
            health=components.health_monitor,
        )
    )
    retention_task = asyncio.create_task(
        run_retention_sweeper(
            config.collection.storage_path,
            config.collection.session_retention_days,
            stop_event,
            interval_seconds=config.daemon.retention_sweep_seconds,
            active_session_path=lambda: storage_writer.paths.root,
            storage_max_mb=config.collection.storage_max_mb,
            storage_warning_mb=config.collection.storage_warning_mb,
            replay_retention_days=config.collection.replay_retention_days,
            active_replay_dir=lambda: _active_replay_dir(components),
        )
    )
    behavior_models_task: asyncio.Task[Any] | None = None
    if live_source is not None:
        # Hot-reload the behavior-models YAML so refit deployments don't
        # require a daemon restart (otherwise a ``label_sessions →
        # refit_behavior_models → upload`` cycle stops here until the
        # operator can ssh in and ``systemctl restart canary``).
        behavior_models_task = asyncio.create_task(
            poll_behavior_models(
                DEFAULT_BEHAVIOR_MODELS_PATH,
                live_source,
                stop_event,
                interval_seconds=config.daemon.model_poll_seconds,
            )
        )
    config_task: asyncio.Task[Any] | None = None
    if live_source is not None:
        # Hot-reload operating-mode config so an operating_modes edit doesn't
        # require a restart (structural changes still do — see poll_config).
        config_task = asyncio.create_task(
            poll_config(
                Path(os.environ.get("CANARY_CONFIG", "canary.yaml")),
                live_source,
                stop_event,
                interval_seconds=config.daemon.model_poll_seconds,
            )
        )
    inference_tasks: list[asyncio.Task[Any]] = []
    if live_source is not None:
        # One unified task scores IF + LSTM + behavior per tick and fuses
        # them (7.2). It owns the LSTM engine/hot-swap internally, so the
        # separate LSTM task is no longer spawned (the standalone
        # ``run_live_inference_lstm`` stays for its direct tests).
        inference_tasks.append(
            asyncio.create_task(run_live_inference(live_source, components, stop_event))
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
            # Bound the drain: if the drain task wedged, queue.join() would block
            # forever and the collector exception below would never reach
            # asyncio.run — the unit would sit "active" with a dead port.
            try:
                await asyncio.wait_for(queue.join(), timeout=DRAIN_SHUTDOWN_SECONDS)
                await asyncio.wait_for(drain_task, timeout=DRAIN_SHUTDOWN_SECONDS)
            except TimeoutError:
                log.warning("telemetry drain shutdown timed out; cancelling")
                drain_task.cancel()
                with suppress(asyncio.CancelledError):
                    await drain_task
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass
            if behavior_models_task is not None:
                behavior_models_task.cancel()
                try:
                    await behavior_models_task
                except asyncio.CancelledError:
                    pass
            if config_task is not None:
                config_task.cancel()
                try:
                    await config_task
                except asyncio.CancelledError:
                    pass
            for task in inference_tasks:
                # Each inference task observes stop_event and returns on its
                # own; this is the join + cancel safety net.
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            components.event_logger.close()
    return components


__all__ = [
    "DRAIN_SHUTDOWN_SECONDS",
    "run_daemon",
]
