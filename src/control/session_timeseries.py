"""Daemon/companion I/O for Task 5.3's historical session time-series read.

:mod:`src.analysis.session_timeseries` is pure pandas over already-fetched
rows; this module resolves the session directory (so a bad id 404s cleanly
instead of silently returning empty arrays) and drives the shared
:class:`~src.storage.telemetry_query.TelemetryQuery` against it. ``root``
is passed in rather than assumed from ``CanaryConfig`` so the identical
function serves both the daemon's ``GET /control/sessions/{id}/chart``
(``config.collection.storage_path``), which in companion mode *is* the
library's ``sessions/`` (``src/dashboard/routes/companion_link.py``; the
standalone companion app and its separate route went in 2026-10-16).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.session_timeseries import compute_time_series
from src.storage.sessions import resolve_session
from src.storage.telemetry_query import TelemetryQuery


def session_time_series_response(
    root: Path,
    session_id: str,
    channels: list[str],
    *,
    resample_hz: float | None = None,
) -> dict[str, Any]:
    """Raises :class:`FileNotFoundError` (caller maps to 404) when
    ``session_id`` doesn't resolve under ``root``."""
    session_dir = resolve_session(root, session_id)
    query = TelemetryQuery(root)
    try:
        rows = query.fetch_channel_rows(channels, session=session_dir.name)
    finally:
        query.close()
    result = compute_time_series(rows, channels=channels, resample_hz=resample_hz)
    result["session_id"] = session_dir.name
    return result


__all__ = ["session_time_series_response"]
