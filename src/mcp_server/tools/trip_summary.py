"""Trip-summary read tool (Task 1.5, engine-analysis-and-mapping.md)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_trip_summary(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """Trip-summary sidecar for one session: duration, distance, fuel used,
    average economy, CO2, idle time/fraction, max/avg speed, 0-60 and
    1/4-mile if a clean launch was captured.

    Computed on demand from the stored session (Task 1.4 — there is no
    session-close hook yet) and cached to ``trip_summary.json`` after the
    first request. 404 (raised as :class:`~src.mcp_server.client.DaemonError`)
    when the session id doesn't resolve or has no telemetry recorded.
    """
    return await client.get_json(f"/sessions/{session_id}/trip-summary")


__all__ = ["get_trip_summary"]
