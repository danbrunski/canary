from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, suppress
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
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.decode.bindings import load_bindings
from src.decode.frame_decoder import FrameDecoder
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

from .components import DaemonComponents, build_components
from .drain import drain_queue_with_broadcast
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
    from src.analysis.gps_tracker import TripEnd
    from src.collection.can_collector import CanReceiveStats
    from src.collection.queueing import CollectorHandoffStats
    from src.models.health import HealthMonitor
    from src.preprocessing.live import LiveFeatureSource
    from src.startup import StartupTimeline

log = logging.getLogger(__name__)

DRAIN_SHUTDOWN_SECONDS = 5.0


def build_live_feature_source(components: DaemonComponents) -> LiveFeatureSource:
    """:func:`src.daemon.inference.build_live_feature_source`, imported on first call.

    ``src.daemon.inference`` pulls in the pandas feature pipeline, the decode
    layer and the scoring worker. Imported at this module's top it loaded on
    the main thread before any collector ran; called from
    :func:`_build_live_source_off_loop` it loads in that thread (S01.3).
    """
    from .inference import build_live_feature_source as _build

    return _build(components)


async def run_live_inference(
    live_source: LiveFeatureSource, components: DaemonComponents, stop_event: asyncio.Event
) -> None:
    """:func:`src.daemon.inference.run_live_inference`, imported on first call.

    Only called once the live source exists, whose build (in a thread) has
    already imported ``src.daemon.inference``; this import is then a lookup.
    """
    from .inference import run_live_inference as _run

    await _run(live_source, components, stop_event)


async def handle_trip_end_off_loop(
    session_dir: Path, config: CanaryConfig, **kwargs: Any
) -> dict[str, Any] | None:
    """:func:`src.control.trip_end.handle_trip_end_off_loop`, imported in a thread.

    ``trip_end`` imports the digest, and with it the fingerprint, surface and
    session-classifier analysis modules: none of it is needed until a trip
    ends, and at this module's top it loaded on the main thread before any
    collector ran (S01.3). The first import is done off the loop so a trip
    that ends early does not stall every sink on a cold read; after that the
    thread hop returns the cached module.
    """
    trip_end = await asyncio.to_thread(importlib.import_module, "src.control.trip_end")
    result: dict[str, Any] | None = await trip_end.handle_trip_end_off_loop(
        session_dir, config, **kwargs
    )
    return result


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
    session_dir: Path,
    config: CanaryConfig,
    components: DaemonComponents,
    trip: TripEnd | None = None,
) -> None:
    """``on_trip_complete`` callback around :func:`handle_trip_end_off_loop`.

    ``session_dir`` is the session the trip *ended* in (the tracker names it;
    D03.29), and ``trip`` becomes the digest's ``trip`` block.

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
            trip=trip,
            emitter=components.emitter,
            notifier=components.notifier,
            catalog_db=config.daemon.catalog_db,
        )
    )
    _TRIP_END_TASKS.add(task)
    task.add_done_callback(_TRIP_END_TASKS.discard)


def shutdown_trip_target(
    components: DaemonComponents, active_session: Path
) -> tuple[Path | None, TripEnd | None]:
    """Where the shutdown fallback digests, and for which trip (D03.29).

    * a trip still open: the session it is in (its stop's session, or where
      it was last seen moving) with its spanned sessions — not the session
      that happens to be active, which after a capture stops is a parked one;
    * no open trip, the active session showed no motion, and the tracker has
      read speed at some point since start: nothing — a digest there describes
      no drive (2026-09-23: the edge digested a 257 s parked session and the
      drive got none). That includes a parked session with no usable reading
      at all (key-off, GPS without a fix, or silence); the caller records a
      ``no_speed_source`` refusal instead if that session was blind for the
      trip-end window;
    * otherwise the active session, as before: an install that has never read
      speed, or a session that did show motion, still gets its report card.
    """
    tracker = components.gps_tracker
    if tracker is None:
        return active_session, None
    pending = tracker.pending_trip()
    if pending is not None:
        return (pending.session or active_session), pending
    if tracker.session_never_moved():
        return None, None
    return active_session, None


async def record_trip_end_unobservable(
    session_dir: Path, config: CanaryConfig, detail: str
) -> None:
    """:func:`src.control.trip_end.record_trip_end_unobservable` in a thread.

    Imported the same lazy way as :func:`handle_trip_end_off_loop`; the write
    is small but it is file I/O plus Parquet footer reads, so it stays off the
    loop. Never raises: a refusal record is a report, not a gate.
    """
    try:
        trip_end = await asyncio.to_thread(importlib.import_module, "src.control.trip_end")
        await asyncio.to_thread(trip_end.record_trip_end_unobservable, session_dir, config, detail)
    except Exception:  # noqa: BLE001 — best-effort, like the digest itself
        log.exception("recording the unobservable trip end for %s failed", session_dir.name)


def _run_trip_unobservable_hook(session_dir: Path, detail: str, config: CanaryConfig) -> None:
    """``on_trip_unobservable`` callback (D03.29): schedule the
    ``no_speed_source`` refusal for ``session_dir`` as a tracked task, like
    :func:`_run_trip_end_hook`, so the trip sink never waits on the write."""
    task = asyncio.create_task(record_trip_end_unobservable(session_dir, config, detail))
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


async def _build_live_source_off_loop(
    components: DaemonComponents, *, enabled: bool
) -> LiveFeatureSource | None:
    """Build the live feature source in a thread, or ``None`` if disabled.

    ~14 s on a cold boot (2026-09-22), reading bindings, DBCs, the DID map and
    the behavior models; run on the loop it stalled every sink while the
    collectors were already filling the queue. Built off the loop it costs
    nothing but the scoring of the records that arrive before it is ready.
    A build that fails leaves the daemon recording without live inference,
    loudly, rather than crash-looping with nothing recorded at all.
    """
    if not enabled:
        return None
    try:
        return await asyncio.to_thread(build_live_feature_source, components)
    except Exception:
        log.exception("could not build the live feature source; recording without live inference")
        return None


async def run_daemon(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter | None = None,
    components: DaemonComponents | None = None,
    enable_live_inference: bool = True,
    startup: StartupTimeline | None = None,
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

    **Collection starts first.** On a cold boot off the SD card the model
    artifacts, the live feature source and the dashboard app took ~60 s to
    build (2026-09-22: collectors started 93 s after the unit), and nothing
    was recorded until they were done. Now the collectors, storage and the
    fanout start before any of them: the model managers are built empty and
    ``poll_model_managers`` loads them off the loop, and the live source and
    the app are built in threads while collection runs. Inference and HTTP
    come up when their inputs are ready; the records before that are stored,
    just not scored.

    ``startup`` is the timeline ``src.main`` began at process start (S01.1);
    without one the health monitor's own is used. Either way it is served as
    ``/status``'s ``startup`` block and logs one summary line once every
    stage this start will reach has been reached.

    Returns the components so a caller can inspect counters / model
    versions for diagnostics or tests.
    """
    components = components or build_components(config, load_models=False)
    if startup is not None:
        components.health_monitor.startup = startup
    timeline = components.health_monitor.startup
    timeline.mark("components_built")
    timeline.expect(
        "collector_tasks_created", "first_record_stored", "models_polled", "app_built", "http_bound"
    )
    if enable_live_inference:
        timeline.expect("live_source_ready")
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
    #
    # Loaded once and shared with the trip tracker (D03.29), which reads the
    # binding declared ``quantity: road_speed`` so a drive with no GPS fix
    # still ends a trip. Same file, same moment as the decoder always loaded.
    bindings = (
        load_bindings(DEFAULT_BINDINGS_PATH)
        if DEFAULT_BINDINGS_PATH.exists()
        and (config.collection.persist_decoded_confidence or components.gps_tracker is not None)
        else None
    )
    if components.gps_tracker is not None:
        components.gps_tracker.use_bindings(bindings)
    storage_frame_decoder = (
        FrameDecoder.from_bindings(
            bindings,
            confidence_levels=config.collection.persist_decoded_confidence,
        )
        if config.collection.persist_decoded_confidence and bindings is not None
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

    # The live source is built after collection starts (below); the drain's
    # live sink joins the fanout when this resolves.
    live_source_ready: asyncio.Future[LiveFeatureSource | None] | None = (
        asyncio.get_running_loop().create_future() if enable_live_inference else None
    )
    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            storage_writer,
            stop_event,
            telemetry_broadcaster=components.telemetry_broadcaster,
            live_source_ready=live_source_ready,
            gps_tracker=components.gps_tracker,
            dtc_state=components.dtc_state,
            dtc_emitter=components.emitter,
            monitor_state=components.monitor_state,
            monitor_emitter=components.emitter,
            vehicle_ledger=components.vehicle_ledger,
            active_session_path=lambda: storage_writer.paths.root,
            on_trip_complete=lambda session_dir, end: _run_trip_end_hook(
                session_dir, config, components, end
            ),
            on_trip_unobservable=lambda session_dir, end: _run_trip_unobservable_hook(
                session_dir, end.detail, config
            ),
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
    # Recording starts here. Everything below it is slow on a cold boot and
    # none of it is needed to record.
    collector_tasks = [
        asyncio.create_task(starter(queue, stop_event)) for starter in active_collectors
    ]
    stop_task = asyncio.create_task(stop_event.wait())
    timeline.mark("collector_tasks_created")
    log.info("collectors started; building live inference and the dashboard")

    behavior_models_task: asyncio.Task[Any] | None = None
    config_task: asyncio.Task[Any] | None = None
    inference_tasks: list[asyncio.Task[Any]] = []
    # The stack enters the dashboard server last, so it exits after the
    # shutdown sequence in ``finally`` (HTTP answers while the daemon winds
    # down, as before) — and a failure while building still runs that
    # sequence, now that collectors are already recording by then.
    async with AsyncExitStack() as stack:
        try:
            live_source = await _build_live_source_off_loop(
                components, enabled=enable_live_inference
            )
            components.live_source = live_source
            if live_source_ready is not None:
                live_source_ready.set_result(live_source)
                if live_source is not None:
                    timeline.mark("live_source_ready")
                else:
                    timeline.skip("live_source_ready", "build failed; recording without it")
            # After the live source, so 7.1's in-memory reset can reach it (and
            # the shared persistence gate / emitter) on replay boundaries.
            components.replay_controller = ReplayController(
                queue=queue,
                writer=storage_writer,
                anomaly_logger=components.event_logger,
                storage_root=config.collection.storage_path,
                live_source=live_source,
                persistence_gate=components.persistence_gate,
                emitter=components.emitter,
            )
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
                # Hot-reload operating-mode config so an operating_modes edit
                # doesn't require a restart (structural changes still do — see
                # poll_config).
                config_task = asyncio.create_task(
                    poll_config(
                        Path(os.environ.get("CANARY_CONFIG", "canary.yaml")),
                        live_source,
                        stop_event,
                        interval_seconds=config.daemon.model_poll_seconds,
                    )
                )
                # One unified task scores IF + LSTM + behavior per tick and
                # fuses them (7.2). It owns the LSTM engine/hot-swap internally
                # — the old standalone LSTM-only task was production-dead and
                # deleted (4.1). Models still loading score as absent.
                inference_tasks.append(
                    asyncio.create_task(run_live_inference(live_source, components, stop_event))
                )

            await stack.enter_async_context(serve_in_background(components))
            done, _ = await asyncio.wait(
                [stop_task, *collector_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop_task:
                    task.result()
        finally:
            if live_source_ready is not None and not live_source_ready.done():
                live_source_ready.set_result(None)
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
                target, open_trip = shutdown_trip_target(components, storage_writer.paths.root)
                if target is not None:
                    await handle_trip_end_off_loop(
                        target,
                        config,
                        trip=open_trip,
                        emitter=components.emitter,
                        notifier=components.notifier,
                    )
                elif components.gps_tracker is not None:
                    # No digest: say so if the session was blind long enough
                    # that a drive could have gone unseen, exactly as a
                    # rotation would have (D03.29).
                    closed = components.gps_tracker.close_session()
                    if closed is not None and not closed.observed:
                        await record_trip_end_unobservable(
                            storage_writer.paths.root, config, closed.detail
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
