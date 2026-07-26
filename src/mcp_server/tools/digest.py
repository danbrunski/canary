"""End-of-drive digest read tool (Task 5.6, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_session_digest(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """End-of-drive digest for one session: duration, mode seconds, gated
    anomaly counts by source, top behavior-surface deviations, fingerprint
    deltas vs. the frozen reference epoch, new DTCs, and new vehicle-ledger
    events.

    Written by the trip-end hook (Task 5.4 — sustained GPS stop, or daemon
    shutdown as a fallback); computed on demand and cached if that hook
    hasn't fired yet for this session. 404 (raised as
    :class:`~src.mcp_server.client.DaemonError`) when the session id doesn't
    resolve or has no telemetry recorded.

    Authenticated (auth-consistency follow-up to Task 5.6): the digest
    aggregates the vehicle ledger and per-session fingerprint z-scores,
    both otherwise only readable via authenticated ``/control/*`` routes
    (``list_vehicle_events`` / ``get_trends``) — ``client`` must carry the
    control token, same as those tools.
    """
    # First hit after a drive computes the digest from the stored session on
    # the Pi (resample + fingerprint + ledger scan) — far beyond the client's
    # 5 s default; cached hits return instantly.
    return await client.get_json(f"/control/sessions/{session_id}/digest", timeout=120.0)


__all__ = ["get_session_digest"]
