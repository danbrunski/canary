"""Daemon-side helper for serving the trip-summary sidecar (Task 1.5,
engine-analysis-and-mapping.md).

Thin wrapper around :func:`~src.analysis.trip_summary_sidecar.get_or_compute_trip_summary`
for the public HTTP endpoint + MCP tool — resolves and validates the session
id the same way :mod:`src.control.classification` does before touching disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.trip_summary_sidecar import get_or_compute_trip_summary
from src.config import CanaryConfig
from src.storage.sessions import resolve_session


def get_trip_summary(config: CanaryConfig, session_id: str) -> dict[str, Any] | None:
    """Return the trip-summary sidecar for one session, computing it on
    first request. ``None`` when the session id doesn't resolve to a real
    live-session directory, or the session has no telemetry recorded yet."""
    storage_path: Path = config.collection.storage_path
    try:
        session_dir = resolve_session(storage_path, session_id)
    except FileNotFoundError:
        return None
    return get_or_compute_trip_summary(session_dir, config)


__all__ = ["get_trip_summary"]
