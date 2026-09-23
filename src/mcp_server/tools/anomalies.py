"""Anomaly event listing."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_recent_anomalies(
    client: DaemonClient,
    *,
    since: str | None = None,
    limit: int = 100,
    source: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Anomaly events from the JSONL log, newest ``limit`` returned oldest-first.

    Parameters
    ----------
    since:
        ISO-8601 timestamp; the daemon returns events with timestamp >= since
        and skips daily log files dated more than a day before it. ``None``
        applies no time bound: the daemon walks the log newest-day-first
        until it has ``limit`` events.
    limit:
        Newest N events, sent to the daemon so it stops reading early. ``0``
        or less removes the cap, and with no ``since`` that parses the whole
        anomaly history inside the collecting daemon.
    source:
        ``"isolation_forest"`` or ``"lstm_autoencoder"`` to filter by detector.
        Applied by the daemon before ``limit`` counts.
    mode:
        Operating mode (``idle``, ``city``, ``highway``, ``wot``, ``off_road``)
        to filter by — useful when a problem only appears in one regime.
        Applied by the daemon before ``limit`` counts.
    """
    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    if source is not None:
        params["source"] = source
    if mode is not None:
        params["mode"] = mode
    if limit > 0:
        params["limit"] = limit
    payload = await client.get_json("/anomalies", params=params or None)
    events: list[dict[str, Any]] = list(payload.get("events", []))
    # The daemon applied all four already; re-applying them is idempotent and
    # keeps the documented shape if it ever ignores one.
    if source is not None:
        events = [e for e in events if e.get("source") == source]
    if mode is not None:
        events = [e for e in events if e.get("operating_mode") == mode]
    if limit > 0:
        # Newest-first slice: the daemon orders oldest-first.
        events = list(reversed(list(reversed(events))[:limit]))
    return {"events": events, "count": len(events)}


__all__ = ["get_recent_anomalies"]
