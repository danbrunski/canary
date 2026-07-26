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
) -> dict[str, Any]:
    """Arm (``enabled=true``) or disarm the CAN transmit gate.

    While armed the daemon is receive-only: broadcast CAN logging, telemetry
    storage, GPS and anomaly scoring all keep running, but no OBD polls and no
    ISO-TP flow-control frames are transmitted.

    Arm this whenever another tool owns a diagnostic session with a module —
    ECU reprogramming, a bidirectional actuator test, a scan-tool routine —
    so CANary's polling cannot contend with it.

    Note that OBD channels go stale while armed, so ``vehicle_state`` will
    read ``engine_off``/``no_data`` and behavior models will stop scoring. That
    is expected, not a fault; ``get_status`` reports ``passive.enabled`` so the
    two are distinguishable. Disarm when the other tool is finished.

    For a guarantee no software path can defeat, additionally put the
    controller in listen-only mode on the Pi (``ip link set can0 type can
    listen-only on``) — that requires an interface cycle and cannot be done
    through this tool.
    """
    body: dict[str, Any] = {"enabled": enabled}
    return await audited_post(
        client, audit, tool="set_passive_mode", path="/control/passive", body=body
    )


__all__ = ["set_passive_mode"]
