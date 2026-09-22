from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.collection import tuning_profile
from src.collection.record_arming import RecordArmingGate
from src.config import CanaryConfig
from src.control.capture import CaptureController
from src.control.capture_finalize import finalize_capture, recompute_quality_off_loop
from src.control.capture_manifest import (
    recover_interrupted_capture,
    stamp_capture_end,
    write_capture_quality,
)
from src.control.replay_control import ReplayController
from src.control.trip_end import handle_trip_end_off_loop
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.decode.frame_decoder import FrameDecoder
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

from .components import DaemonComponents, build_components
from .drain import drain_queue_with_broadcast
from .inference import build_live_feature_source, run_live_inference
from .online_training import run_online_training_scheduler
from .polling import (
    poll_behavior_models,
    poll_bus_budget_ttl,
    poll_config,
    poll_model_managers,
    poll_passive_ttl,
)
from .retention import _active_replay_dir, run_retention_sweeper
from .server import serve_in_background

if TYPE_CHECKING:
    from src.collection.can_collector import CanReceiveStats
    from src.collection.queueing import CollectorHandoffStats
    from src.models.health import HealthMonitor

log = logging.getLogger(__name__)

DRAIN_SHUTDOWN_SECONDS = 5.0

#: How long shutdown waits for a trip-end digest started at the GPS edge to
#: finish before cancelling it (which terminates its child). Bounded so a
#: multi-gigabyte session cannot hold the unit in `deactivating` — the digest
#: is on-demand anyway, so a cancelled one is recomputed by the next read.
TRIP_END_SHUTDOWN_SECONDS = 30.0

__all__ = [
    "DRAIN_SHUTDOWN_SECONDS",
    "TRIP_END_SHUTDOWN_SECONDS",
    "run_daemon",
    "run_retention_sweeper",
    "serve_in_background",
]

#: Strong references to in-flight trip-end digests. ``create_task`` alone
#: only weakly references its task, so without this the GC may collect a
#: digest mid-computation.
_TRIP_END_TASKS: set[asyncio.Task[Any]] = set()


def _run_trip_end_hook(
    session_dir: Path, config: CanaryConfig, components: DaemonComponents
) -> None:
    """``on_trip_ended`` callback shape (``Callable[[Path], None]``) around
    :func:`handle_trip_end_off_loop`.

    Returns as soon as the digest is *scheduled*, not when it finishes. This
    callback runs inside the GPS sink, and a digest is a full-session load —
    measured at 5,483 MB peak for a 2,622,370-row session, minutes of CPU.
    Computed inline it wedged the daemon twice (OOM-killed 2026-09-11,
    unresponsive for 38 minutes 2026-09-17), so it goes to a tracked
    background task which awaits a capped child process; the GPS sink is not
    made to wait on it either. ``handle_trip_end_off_loop`` never raises and
    records its own refusals, so nothing here has a result to inspect.
    """
    task = asyncio.create_task(
        handle_trip_end_off_loop(
            session_dir,
            config,
            emitter=components.emitter,
            notifier=components.notifier,
            catalog_db=config.daemon.catalog_db,
        )
    )
    _TRIP_END_TASKS.add(task)
    task.add_done_callback(_TRIP_END_TASKS.discard)


async def _settle_trip_end_tasks(timeout: float = TRIP_END_SHUTDOWN_SECONDS) -> None:
    """Let in-flight trip-end digests finish, bounded; cancel the rest.

    Cancelling propagates into ``compute_digest_off_loop``'s single-use pool,
    which terminates the child — otherwise a several-GB worker would outlive
    the daemon that spawned it.
    """
    pending = set(_TRIP_END_TASKS)
    if not pending:
        return
    done, still_running = await asyncio.wait(pending, timeout=timeout)
    del done
    for task in still_running:
        task.cancel()
    if still_running:
        log.warning("cancelled %d in-flight trip-end digest(s) at shutdown", len(still_running))
        await asyncio.gather(*still_running, return_exceptions=True)


def _obd_handoff_stats_observer(
    health: HealthMonitor,
) -> Callable[[CollectorHandoffStats], None]:
    """Adapt the Canable collector's handoff counters (C03.6) into ``/status``."""

    def _observe(stats: CollectorHandoffStats) -> None:
        health.observe_collector_handoff(
            "obd_collect",
            accepted=stats.handoff.accepted,
            buffer_dropped=stats.handoff.dropped,
            buffer_high_water=stats.handoff.high_water,
            buffer_capacity=stats.handoff.capacity,
            queue_dropped=stats.queue_dropped,
            batches=stats.handoff.drains,
            raw_responses=stats.raw_responses,
            derived_records=stats.derived_records,
            reconnects=stats.reconnects,
        )

    return _observe


def _can_receive_stats_observer(health: HealthMonitor) -> Callable[[CanReceiveStats], None]:
    """Adapt the passive CAN receive counters (C07.5) into ``/status``."""

    def _observe(stats: CanReceiveStats) -> None:
        health.observe_receive_path(
            "can_receive",
            received=stats.frames,
            parse_errors=stats.parse_errors,
            buffer_dropped=stats.handoff.dropped,
            buffer_high_water=stats.handoff.high_water,
            buffer_capacity=stats.handoff.capacity,
            queue_dropped=stats.queue_dropped,
            batches=stats.handoff.drains,
            reconnects=stats.reconnects,
        )

    return _observe


async def run_daemon(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter | None = None,
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
      * ``poll_passive_ttl`` → auto-disarms ``tx_gate`` passive mode once a
        caller-supplied TTL expires
      * ``run_retention_sweeper`` → age + byte-cap + low-space retention
      * ``run_online_training_scheduler`` (Task 3.2) → config-gated, default
        disabled; scheduled retrain via the existing subprocess-isolated
        ``JobController``, never on this event loop
      * the dashboard via ``serve_in_background``

    Returns the components so a caller can inspect counters / model
    versions for diagnostics or tests.
    """
    components = components or build_components(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=config.collection.queue_max_size)
    arming_config = config.collection.record_arming
    # Task 8.6: config-gated, defaults off (RecordArmingConfig.enabled=False)
    # — an unconfigured/disabled daemon opens its writer exactly as before,
    # no gate object at all. Only applies to the writer this call builds
    # itself; a caller-supplied ``writer`` (tests, replay) is used as-is.
    arming_gate = (
        RecordArmingGate(
            arm_sources=arming_config.arm_sources,
            arm_channels=arming_config.arm_channels,
            arm_threshold=arming_config.arm_threshold,
            disarm_after_seconds=arming_config.disarm_after_seconds,
        )
        if arming_config.enabled
        else None
    )
    storage_writer = ensure_async_writer(
        ParquetTelemetryWriter(config.collection.storage_path, arming_gate=arming_gate)
        if writer is None
        else writer
    )
    components.capture_controller = CaptureController(lambda: storage_writer.paths.root)
    # Task 1.1 follow-up: give run_live_inference the same writer instance
    # drain_queue_with_broadcast wires into run_storage_sink below, so derived
    # channels land in the same Parquet session as everything else.
    components.storage_writer = storage_writer

    interrupted = recover_interrupted_capture(config.collection.storage_path)
    if interrupted is not None:
        interrupted_dir, _manifest = interrupted
        try:
            quality = await recompute_quality_off_loop(interrupted_dir)
            write_capture_quality(interrupted_dir, quality.to_dict())
        except Exception:  # noqa: BLE001 — recovery is best effort, stamp remains truthful
            log.exception("failed to finalize interrupted capture %s", interrupted_dir)

    # Build the live source (no-op if bindings/behavior_models are absent on
    # disk — observe still works, just without decoded:* / deviation:* columns).
    # Built before the ReplayController below so 7.1's in-memory reset can
    # reach it (and the shared persistence gate / emitter) on replay boundaries.
    live_source = build_live_feature_source(components) if enable_live_inference else None
    components.live_source = live_source
    components.replay_controller = ReplayController(
        queue=queue,
        writer=storage_writer,
        anomaly_logger=components.event_logger,
        storage_root=config.collection.storage_path,
        live_source=live_source,
        persistence_gate=components.persistence_gate,
        emitter=components.emitter,
    )

    # Lazy import — keeps daemon module importable without the collector deps
    # at module-load time. Task 4.7: this used to reach into `src.main`
    # (a daemon → main import inversion — `main.py` is the CLI entry point,
    # it should depend on the daemon package, not the other way around);
    # `default_collectors` now lives in `src.collection` instead.
    from src.collection.collectors import default_collectors as _default_collectors

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
            freeze_frame_trigger=components.freeze_frame_trigger,
            force_poll_trigger=components.force_poll_trigger,
            can_receive_stats_callback=_can_receive_stats_observer(components.health_monitor),
            obd_handoff_stats_callback=_obd_handoff_stats_observer(components.health_monitor),
        )

    # Decoded-channel persistence (config-gated): bound CAN signals at the
    # configured confidence levels are stored as queryable ``decoded:*`` rows
    # next to their raw frames. Absent bindings file → decoder is None and
    # only raw frames are persisted, exactly as before.
    storage_frame_decoder = (
        FrameDecoder.from_path(
            DEFAULT_BINDINGS_PATH,
            confidence_levels=config.collection.persist_decoded_confidence,
        )
        if config.collection.persist_decoded_confidence
        else None
    )
    if storage_frame_decoder is not None:
        log.info(
            "decoded-channel persistence enabled",
            extra={
                "signals": storage_frame_decoder.signal_count,
                "confidence": list(config.collection.persist_decoded_confidence),
            },
        )

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            storage_writer,
            stop_event,
            telemetry_broadcaster=components.telemetry_broadcaster,
            live_source=live_source,
            gps_tracker=components.gps_tracker,
            dtc_state=components.dtc_state,
            dtc_emitter=components.emitter,
            monitor_state=components.monitor_state,
            monitor_emitter=components.emitter,
            vehicle_ledger=components.vehicle_ledger,
            active_session_path=lambda: storage_writer.paths.root,
            on_trip_ended=lambda session_dir: _run_trip_end_hook(session_dir, config, components),
            storage_path=config.collection.storage_path,
            storage_warning_mb=config.collection.storage_warning_mb,
            queue_warning_fraction=config.collection.queue_warning_fraction,
            health_monitor=components.health_monitor,
            storage_decode=(
                (
                    lambda record: (
                        storage_frame_decoder.decode(record)
                        if tuning_profile.broadcast_decode_enabled()
                        else ()
                    )
                )
                if storage_frame_decoder is not None
                else None
            ),
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
    passive_ttl_task = asyncio.create_task(poll_passive_ttl(stop_event))
    bus_budget_ttl_task = asyncio.create_task(poll_bus_budget_ttl(stop_event))
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
            catalog_db=config.daemon.catalog_db,
        )
    )
    online_training_task: asyncio.Task[Any] | None = None
    if config.online_training.enabled and components.job_controller is not None:
        # Task 3.2: config-gated, defaults off (see OnlineTrainingConfig). Only
        # created when explicitly enabled — an unconfigured/disabled daemon
        # behaves exactly as before, no new background task at all.
        online_training_task = asyncio.create_task(
            run_online_training_scheduler(
                components.job_controller,
                stop_event,
                interval_seconds=config.online_training.interval_seconds,
                window_days=config.online_training.window_days,
                poll_interval_seconds=config.online_training.poll_interval_seconds,
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
                config.daemon.behavior_models,
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
        # them (7.2). It owns the LSTM engine/hot-swap internally — the old
        # standalone LSTM-only task was production-dead and deleted (4.1).
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
            if tuning_profile.is_active():
                capture_dir = Path(storage_writer.paths.root)
                await finalize_capture(
                    capture_dir,
                    health=components.health_monitor,
                    writer=storage_writer,
                    start=None,
                )
                stamp_capture_end(capture_dir, "daemon_shutdown")
            # Task 5.4 fallback trip-end trigger: the drive can end because
            # the daemon stopped (Pi shutdown) rather than a clean
            # GPS-detected stop. Settle whatever the GPS edge already
            # started first — the fallback is idempotent against a *written*
            # digest.json, and `_IN_FLIGHT` against one still computing, so
            # this is a no-op when the edge already fired for the active
            # session. Off-loop like the edge path: a capped child, never a
            # multi-gigabyte load inside the shutdown sequence itself.
            try:
                await _settle_trip_end_tasks()
                await handle_trip_end_off_loop(
                    storage_writer.paths.root,
                    config,
                    emitter=components.emitter,
                    notifier=components.notifier,
                )
            except Exception:  # noqa: BLE001 — shutdown must never fail on this
                log.exception("trip-end fallback (shutdown) failed")
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            passive_ttl_task.cancel()
            try:
                await passive_ttl_task
            except asyncio.CancelledError:
                pass
            bus_budget_ttl_task.cancel()
            try:
                await bus_budget_ttl_task
            except asyncio.CancelledError:
                pass
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass
            if online_training_task is not None:
                online_training_task.cancel()
                try:
                    await online_training_task
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
