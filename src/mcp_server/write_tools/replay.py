"""Replay-control write tools — start/stop session replay against the live daemon.

The daemon-side controller (:mod:`src.control.replay_control`) isolates
replay outputs under ``_replay-*`` paths so they never contaminate
training data or the real anomaly log. These tools wrap the HTTP control
endpoints and audit every invocation with ``actor="mcp"``.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def start_replay(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
    speed: float = 1.0,
    loop: bool = False,
) -> dict[str, Any]:
    """Replay a stored session into the live capture pipeline.

    Pushes the recorded session's telemetry onto the same queue as a real
    drive, so IF + LSTM + behavior detectors fire as if the truck were
    running. Parquet writes land under ``<storage>/_replay-<id>/`` and
    anomalies under ``<anomalies_dir>/_replay/<id>.jsonl`` — both
    excluded from training discovery and feedback loops.

    Parameters
    ----------
    session_id:
        Directory name of an existing live session (e.g.
        ``"2026-05-18T033527"``). Replay-of-replays is rejected.
    speed:
        Multiplier on the original recording's pacing. ``1.0`` real-time;
        ``3.0`` three times faster.
    loop:
        Loop the recording until ``stop_replay`` is called. Default
        ``False`` so the replay halts on its own when the source exhausts.
    """
    body: dict[str, Any] = {"session_id": session_id, "speed": speed, "loop": loop}
    try:
        result = await client.post_json("/control/replay", body)
    except DaemonError as exc:
        audit.record(tool="start_replay", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="start_replay", result="ok", payload={**body, "response": result})
    return result


async def stop_replay(
    client: DaemonClient,
    audit: MCPAuditLogger,
) -> dict[str, Any]:
    """Cancel the in-flight replay and roll the writer/logger back to live."""
    try:
        result = await client.post_json("/control/replay/stop", {})
    except DaemonError as exc:
        audit.record(tool="stop_replay", result="error", payload={"error": str(exc)})
        raise
    audit.record(tool="stop_replay", result="ok", payload={"response": result})
    return result


async def get_replay_status(client: DaemonClient) -> dict[str, Any]:
    """Return the current replay state (pure read, no audit)."""
    return await client.get_json("/control/replay")


__all__ = ["start_replay", "stop_replay", "get_replay_status"]
