"""Daemon package public API.

The old ``src.daemon`` module is kept as this facade so callers can continue
importing the same symbols while implementation lives in responsibility-focused
modules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config import CanaryConfig
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter

from . import runtime as _runtime
from . import server as _server
from .components import DaemonComponents, build_components
from .drain import (
    TelemetrySink,
    _record_to_dict,
    drain_queue_with_broadcast,
    run_gps_sink,
    run_live_source_sink,
    run_storage_sink,
    run_telemetry_broadcast_sink,
    run_telemetry_fanout,
)
from .polling import poll_behavior_models, poll_config, poll_model_managers
from .retention import run_retention_sweeper
from .server import serve_in_background as _serve_in_background

if TYPE_CHECKING:
    import uvicorn

    from src.startup import StartupTimeline

    from .inference import (
        _finite_or_none,
        _observe_behavior,
        _publish_score,
        _score_and_emit,
        _sequence_observe,
        build_live_feature_source,
        run_live_inference,
    )

#: Re-exported from :mod:`src.daemon.inference`, which is imported on first
#: access rather than with this package: it pulls in the pandas feature
#: pipeline, the decode layer and the scoring worker, and ``import src.daemon``
#: runs on the main thread before any collector does (S01.3). The daemon
#: itself reaches it through :func:`src.daemon.runtime.build_live_feature_source`,
#: in the thread that builds the live source.
_INFERENCE_EXPORTS = frozenset(
    {
        "_finite_or_none",
        "_observe_behavior",
        "_publish_score",
        "_score_and_emit",
        "_sequence_observe",
        "build_live_feature_source",
        "run_live_inference",
    }
)


def __getattr__(name: str) -> Any:
    if name in _INFERENCE_EXPORTS:
        from . import inference as _inference

        value = getattr(_inference, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


DASHBOARD_SHUTDOWN_SECONDS = _server.DASHBOARD_SHUTDOWN_SECONDS
DRAIN_SHUTDOWN_SECONDS = _runtime.DRAIN_SHUTDOWN_SECONDS


@asynccontextmanager
async def serve_in_background(
    components: DaemonComponents,
    *,
    server_factory: Callable[..., uvicorn.Server] | None = None,
) -> AsyncIterator[uvicorn.Server]:
    """Run the dashboard in the background.

    Wrapper keeps monkeypatches against ``src.daemon.DASHBOARD_SHUTDOWN_SECONDS``
    effective for existing tests and external callers.
    """
    _server.DASHBOARD_SHUTDOWN_SECONDS = DASHBOARD_SHUTDOWN_SECONDS
    async with _serve_in_background(components, server_factory=server_factory) as server:
        yield server


async def run_daemon(
    config: CanaryConfig,
    stop_event: Any,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter | None = None,
    components: DaemonComponents | None = None,
    enable_live_inference: bool = True,
    startup: StartupTimeline | None = None,
    passive_state: Path | None = None,
) -> DaemonComponents:
    """Run the full daemon while preserving the historic ``src.daemon`` facade."""
    _runtime.run_retention_sweeper = run_retention_sweeper
    _runtime.serve_in_background = serve_in_background
    _runtime.DRAIN_SHUTDOWN_SECONDS = DRAIN_SHUTDOWN_SECONDS
    return await _runtime.run_daemon(
        config,
        stop_event,
        collectors=collectors,
        writer=writer,
        components=components,
        enable_live_inference=enable_live_inference,
        startup=startup,
        passive_state=passive_state,
    )


__all__ = [
    "DASHBOARD_SHUTDOWN_SECONDS",
    "DRAIN_SHUTDOWN_SECONDS",
    "DaemonComponents",
    "TelemetrySink",
    "build_components",
    "build_live_feature_source",
    "drain_queue_with_broadcast",
    "poll_behavior_models",
    "poll_config",
    "poll_model_managers",
    "run_daemon",
    "run_gps_sink",
    "run_live_source_sink",
    "run_live_inference",
    "run_retention_sweeper",
    "run_storage_sink",
    "run_telemetry_broadcast_sink",
    "run_telemetry_fanout",
    "serve_in_background",
    "_finite_or_none",
    "_sequence_observe",
    "_observe_behavior",
    "_publish_score",
    "_record_to_dict",
    "_score_and_emit",
]
