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
from src.storage.session_products import read_digest_skipped
from src.storage.sessions import resolve_session


def get_session_digest(
    config: CanaryConfig, session_id: str, *, catalog_db: Path | None = None
) -> dict[str, Any] | None:
    """Return the digest sidecar for one session, computing it on first
    request if the trip-end hook (Task 5.4) hasn't already written it.
    ``None`` when the session id doesn't resolve to a real live-session
    directory, the session has no telemetry recorded yet, or the digest was
    *refused* — ask :func:`get_session_digest_refusal` which of those it was.
    ``catalog_db`` (Task 6.3) re-indexes the session into the catalog on a
    freshly-computed digest, matching the trip-end hook's own wiring."""
    storage_path: Path = config.collection.storage_path
    try:
        session_dir = resolve_session(storage_path, session_id)
    except FileNotFoundError:
        return None
    return get_or_compute_digest(session_dir, config, catalog_db=catalog_db)


def get_session_digest_refusal(config: CanaryConfig, session_id: str) -> dict[str, Any] | None:
    """The ``digest_skipped.json`` payload for one session, if there is one.

    Lets the route answer "refused, here is why" instead of the bare 404 a
    missing digest would otherwise get — a refusal (over the row budget, or
    the child ran out of address space) is an answer about the session, not
    an absence of one."""
    storage_path: Path = config.collection.storage_path
    try:
        session_dir = resolve_session(storage_path, session_id)
    except FileNotFoundError:
        return None
    return read_digest_skipped(session_dir)


__all__ = ["get_session_digest", "get_session_digest_refusal"]
