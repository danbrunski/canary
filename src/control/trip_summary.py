"""Daemon-side helper for serving the trip-summary sidecar (Task 1.5,
engine-analysis-and-mapping.md).

Thin wrapper around :func:`~src.analysis.trip_summary_sidecar.get_or_compute_trip_summary`
for the public HTTP endpoint + MCP tool — resolves and validates the session
id the same way :mod:`src.control.classification` does before touching disk.

The first request for a session computes the sidecar from the whole stored
session (``resample_and_segment``), which is whole-session analysis and so
never runs in the daemon's own process (AGENTS.md, "No analysis on the Pi while
it collects"; R29.1). :func:`trip_summary_off_loop` is what a route calls: a
sidecar already on disk is served directly, a missing one is computed in the
workbench's bounded child (job kind ``trip_summary``) and refused with
``analysis_deferred`` while a capture is recording.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.analysis.trip_summary_sidecar import (
    SCHEMA_VERSION,
    get_or_compute_trip_summary,
    read_trip_summary,
)
from src.config import CanaryConfig
from src.control.workbench_jobs import capture_deferral, run_off_loop
from src.storage.sessions import resolve_session


def get_trip_summary(config: CanaryConfig, session_id: str) -> dict[str, Any] | None:
    """Return the trip-summary sidecar for one session, computing it on
    first request. ``None`` when the session id doesn't resolve to a real
    live-session directory, or the session has no telemetry recorded yet.

    Computes in the calling process: this is the workbench child's entry for
    the ``trip_summary`` job kind (and ``scripts/trip_report.py``'s). A route
    calls :func:`trip_summary_off_loop`."""
    storage_path: Path = config.collection.storage_path
    try:
        session_dir = resolve_session(storage_path, session_id)
    except FileNotFoundError:
        return None
    return get_or_compute_trip_summary(session_dir, config)


def _stored_trip_summary(session_dir: Path) -> dict[str, Any] | None:
    """The sidecar if it is on disk at the current schema version; the cheap path."""
    cached = read_trip_summary(session_dir)
    if cached is not None and cached.get("schema_version") == SCHEMA_VERSION:
        return cached
    return None


async def trip_summary_off_loop(config: CanaryConfig, session_id: str) -> dict[str, Any] | None:
    """The trip-summary sidecar for one session, never computed in this process.

    A current sidecar is read and served (a small file, even during a capture).
    A missing or stale one is computed in the workbench's bounded child, after
    ``capture_deferral``. Returns ``None`` for an id that is not a session or a
    session with no telemetry; raises
    :class:`~src.control.workbench_jobs.WorkbenchRefusedError` for a deferral,
    a busy child or a child that failed (the route's 409 / 503).
    """
    try:
        session_dir = resolve_session(config.collection.storage_path, session_id)
    except FileNotFoundError:
        return None
    stored = await asyncio.to_thread(_stored_trip_summary, session_dir)
    if stored is not None:
        return stored
    capture_deferral(config, [session_id])
    result = await run_off_loop("trip_summary", {"session_id": session_id}, config)
    payload: dict[str, Any] | None = result["trip_summary"]
    return payload


__all__ = ["get_trip_summary", "trip_summary_off_loop"]
