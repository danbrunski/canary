"""Daemon-side helper for serving the digest sidecar (Task 5.6,
eval-longitudinal-and-dtc.md).

Thin wrapper around :func:`~src.analysis.digest.get_or_compute_digest` for
the public HTTP endpoint + MCP tool — resolves and validates the session id
the same way :mod:`src.control.trip_summary` does before touching disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.analysis.digest import get_or_compute_digest
from src.config import CanaryConfig
from src.control.sessions import resolve_session


def get_session_digest(
    config: CanaryConfig, session_id: str, *, catalog_db: Path | None = None
) -> dict[str, Any] | None:
    """Return the digest sidecar for one session, computing it on first
    request if the trip-end hook (Task 5.4) hasn't already written it.
    ``None`` when the session id doesn't resolve to a real live-session
    directory, or the session has no telemetry recorded yet. ``catalog_db``
    (Task 6.3) re-indexes the session into the catalog on a freshly-computed
    digest, matching the trip-end hook's own wiring."""
    storage_path: Path = config.collection.storage_path
    try:
        session_dir = resolve_session(storage_path, session_id)
    except FileNotFoundError:
        return None
    return get_or_compute_digest(session_dir, config, catalog_db=catalog_db)


__all__ = ["get_session_digest"]
