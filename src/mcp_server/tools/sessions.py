"""Session and dataset/training-run read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def list_sessions(
    client: DaemonClient,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """All known sessions on disk, oldest-first.

    Use this for navigation; combine with ``get_session`` to drill into one.
    """
    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    if limit is not None:
        params["limit"] = limit
    return await client.get_json("/control/sessions", params=params or None)


async def get_session(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """Detail for one session: tags, control.json, chunk count, metadata."""
    return await client.get_json(f"/control/sessions/{session_id}")


async def get_session_timeseries(
    client: DaemonClient,
    *,
    session_id: str,
    channels: list[str],
    resample_hz: float = 1.0,
) -> dict[str, Any]:
    """Aligned selected channels without exporting the session's Parquet."""
    return await client.get_json(
        f"/control/sessions/{session_id}/chart",
        params={"channels": ",".join(channels), "resample_hz": resample_hz},
        timeout=60.0,
    )


async def get_dataset_health(client: DaemonClient) -> dict[str, Any]:
    """Corpus readiness: per-mode sample counts, gaps, last-seen times.

    Useful when deciding whether retraining now is worthwhile.
    """
    return await client.get_json("/control/datasets/health")


async def get_training_runs(client: DaemonClient) -> dict[str, Any]:
    """Training-run history (the latest record per run from the ledger)."""
    return await client.get_json("/control/training-runs")


async def get_current_capture(client: DaemonClient) -> dict[str, Any]:
    """Read the currently active capture window (pure read).

    ``active`` is the field to check: with no window open this answers 200
    with ``capture: null``, ``active: false`` and a ``detail`` naming the
    cause (a daemon restart drops the in-memory window while recording
    continues). Do not read a null ``capture`` as "the call failed" or as
    "a window is open" — it is neither.
    """
    return await client.get_json("/control/captures/current")


__all__ = [
    "list_sessions",
    "get_session",
    "get_session_timeseries",
    "get_dataset_health",
    "get_training_runs",
    "get_current_capture",
]
