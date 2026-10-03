from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from .components import DaemonComponents

if TYPE_CHECKING:
    import uvicorn

    from src.startup import StartupTimeline

log = logging.getLogger(__name__)

DASHBOARD_SHUTDOWN_SECONDS = 5.0


def _build_app(components: DaemonComponents) -> Any:
    """Import the dashboard API and build the app; called in a thread.

    uvicorn is imported here too, off the loop, so the import in
    :func:`serve_in_background` is a ``sys.modules`` lookup.
    """
    import uvicorn  # noqa: F401

    from src.dashboard.api import create_app
    from src.models.feedback import FeedbackStore

    # R03.5: the routes must drive the *same* capture lifecycle the shutdown
    # path stops, or an HTTP-started capture's start epoch lives in a session
    # shutdown never sees (loss_epoch_start: None). `run_daemon` builds both
    # right after the writer; with a writer wired, refuse rather than let
    # create_app build a private pair. (No writer: captures are unavailable
    # anyway, as in a bare component record.)
    if components.storage_writer is not None and (
        components.profile_capture is None or components.capture_autostart is None
    ):
        raise RuntimeError(
            "daemon app built before run_daemon wired profile_capture/capture_autostart"
        )
    return create_app(
        components.config,
        anomalies_dir=components.config.daemon.anomalies_dir,
        models_dir=components.config.daemon.models_dir,
        behavior_models_path=components.config.daemon.behavior_models,
        accepted_baselines_path=components.config.daemon.accepted_baselines,
        telemetry_broadcaster=components.telemetry_broadcaster,
        anomalies_broadcaster=components.anomalies_broadcaster,
        scores_broadcaster=components.scores_broadcaster,
        control_broadcaster=components.control_broadcaster,
        emitter=components.emitter,
        capture_controller=components.capture_controller,
        job_controller=components.job_controller,
        replay_controller=components.replay_controller,
        storage_writer=components.storage_writer,
        audit_logger=components.audit_logger,
        feedback_store=FeedbackStore(components.config.daemon.feedback_log),
        vehicle_ledger_store=components.vehicle_ledger,
        alert_tuner=components.alert_tuner,
        health_monitor=components.health_monitor,
        behavior_live=components.behavior_live,
        dtc_state=components.dtc_state,
        monitor_state=components.monitor_state,
        gps_tracker=components.gps_tracker,
        sequence_detector_controller=components.sequence_detector_controller,
        force_poll_trigger=components.force_poll_trigger,
        inference_timing=components.inference_timing,
        live_source=components.live_source,
        profile_capture=components.profile_capture,
        capture_autostart=components.capture_autostart,
    )


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
    # Building the app is ~11 s on a cold boot (2026-09-22). Collection is
    # already running by now, so do it in a thread rather than stall the loop
    # every sink runs on — including the import of the API module tree
    # (FastAPI and every route's dependencies), which used to happen at this
    # module's import, on the main thread, before any collector ran (S01.3).
    app = await asyncio.to_thread(_build_app, components)
    startup = components.health_monitor.startup
    startup.mark("app_built")
    if components.job_controller is not None:
        loop = asyncio.get_running_loop()
        components.job_controller.set_event_callback(
            lambda event: components.control_broadcaster.publish_threadsafe(loop, event)
        )
    import uvicorn  # already loaded by _build_app, in the thread

    if server_factory is None:
        server_factory = uvicorn.Server
    server_config = uvicorn.Config(
        app,
        host=components.config.dashboard.host,
        port=components.config.dashboard.port,
        log_level="info",
        lifespan="on",
        # Cap uvicorn's own graceful shutdown so it stops waiting on lingering
        # background tasks (e.g. an open /ws/anomalies WebSocket) instead of
        # hanging "Waiting for background tasks to complete" forever.
        timeout_graceful_shutdown=int(DASHBOARD_SHUTDOWN_SECONDS),
    )
    server = server_factory(config=server_config)
    serve_task = asyncio.create_task(server.serve())
    bound_task = asyncio.create_task(_mark_http_bound(server, serve_task, startup))
    # uvicorn flips server.started True once the listening socket is open.
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    try:
        yield server
    finally:
        bound_task.cancel()
        with suppress(asyncio.CancelledError):
            await bound_task
        server.should_exit = True
        # Belt-and-suspenders around timeout_graceful_shutdown: if uvicorn still
        # doesn't return (older versions, a wedged lifespan task), force it down
        # so the daemon can finish exiting rather than pinning the unit "active".
        try:
            await asyncio.wait_for(serve_task, timeout=DASHBOARD_SHUTDOWN_SECONDS + 2.0)
        except TimeoutError:
            log.warning("dashboard graceful shutdown timed out; forcing server down")
            server.force_exit = True
            serve_task.cancel()
            with suppress(asyncio.CancelledError):
                await serve_task


async def _mark_http_bound(
    server: uvicorn.Server, serve_task: asyncio.Task[Any], startup: StartupTimeline
) -> None:
    """Mark ``http_bound`` (S01.1) when uvicorn has opened its socket.

    Separate from the bounded wait above, which gives up after 5 s and
    carries on: a slow cold start still gets its stage recorded.
    """
    while not serve_task.done():
        if getattr(server, "started", False):
            startup.mark("http_bound")
            return
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------

__all__ = [
    "DASHBOARD_SHUTDOWN_SECONDS",
    "serve_in_background",
]
