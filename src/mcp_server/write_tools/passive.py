"""Passive-mode (CAN transmit gate) write tool."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def set_passive_mode(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    enabled: bool,
    ttl_seconds: float | None = None,
) -> dict[str, Any]:
    """Arm (``enabled=true``) or disarm the CAN transmit gate.

    While armed the daemon is receive-only: broadcast CAN logging, telemetry
    storage, GPS and anomaly scoring all keep running, but no OBD polls and no
    ISO-TP flow-control frames are transmitted.

    Arm this whenever another tool owns a diagnostic session with a module —
    ECU reprogramming, a bidirectional actuator test, a scan-tool routine —
    so CANary's polling cannot contend with it.

    ``ttl_seconds`` (only meaningful when arming, capped at 3600) auto-disarms
    after that many seconds even if nobody calls this again — the daemon
    polls for expiry in the background. Worth setting whenever the window is
    known in advance (a timed reflash, a fixed-length scan-tool routine): it
    is one less thing to remember, and a died session or an interrupted
    conversation can't leave OBD polling suppressed indefinitely. Omit for
    the old "stays armed until manually disarmed" behavior.

    Note that OBD channels go stale while armed, so ``vehicle_state`` will
    read ``engine_off``/``parked``/``no_data`` and behavior models will stop scoring. That
    is expected, not a fault; ``get_status`` reports ``passive.enabled`` (and
    ``passive.ttl_remaining_seconds`` when a TTL is set) so both are
    distinguishable from a real fault. Disarm when the other tool is
    finished, or let the TTL do it.

    For a guarantee no software path can defeat, additionally put the
    controller in listen-only mode on the Pi (``ip link set can0 type can
    listen-only on``) — that requires an interface cycle and cannot be done
    through this tool.
    """
    body: dict[str, Any] = {"enabled": enabled}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    return await audited_post(
        client, audit, tool="set_passive_mode", path="/control/passive", body=body
    )


__all__ = ["set_passive_mode"]
