from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

import uvicorn

from src.alerts.feedback import FeedbackStore
from src.dashboard.api import create_app

from .components import DaemonComponents

log = logging.getLogger(__name__)

DASHBOARD_SHUTDOWN_SECONDS = 5.0


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
    )
    if components.job_controller is not None:
        loop = asyncio.get_running_loop()
        components.job_controller.set_event_callback(
            lambda event: components.control_broadcaster.publish_threadsafe(loop, event)
        )
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
    # uvicorn flips server.started True once the listening socket is open.
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    try:
        yield server
    finally:
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


# ---------------------------------------------------------------------------

__all__ = [
    "DASHBOARD_SHUTDOWN_SECONDS",
    "serve_in_background",
]
