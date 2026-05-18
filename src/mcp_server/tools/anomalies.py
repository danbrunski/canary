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
    """Anomaly events from the JSONL log.

    Parameters
    ----------
    since:
        ISO-8601 timestamp; the daemon returns events with timestamp >= since.
        ``None`` returns the daemon default (today, typically).
    limit:
        Client-side cap. The daemon doesn't paginate; we slice after fetch.
    source:
        ``"isolation_forest"`` or ``"lstm_autoencoder"`` to filter by detector.
    mode:
        Operating mode (``idle``, ``city``, ``highway``, ``wot``, ``off_road``)
        to filter by — useful when a problem only appears in one regime.
    """
    params: dict[str, Any] = {}
    if since is not None:
        params["since"] = since
    payload = await client.get_json("/anomalies", params=params or None)
    events: list[dict[str, Any]] = list(payload.get("events", []))
    if source is not None:
        events = [e for e in events if e.get("source") == source]
    if mode is not None:
        events = [e for e in events if e.get("operating_mode") == mode]
    if limit > 0:
        # Newest-first slice so the most recent N survive truncation. The
        # daemon orders oldest-first; reverse, slice, then restore order.
        events = list(reversed(list(reversed(events))[:limit]))
    return {"events": events, "count": len(events)}


__all__ = ["get_recent_anomalies"]
