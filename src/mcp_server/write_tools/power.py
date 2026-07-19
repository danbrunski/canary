"""UPS/power write tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def set_ups_keepalive(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    minutes: float,
) -> dict[str, Any]:
    """Set (``minutes`` > 0) or cancel (``minutes`` = 0) the UPS keep-alive hold.

    Use before a bench move longer than the watchdog's grace window (default
    5 min) — e.g. unplugging to carry the Pi somewhere for an extended
    session. While the hold is active the watchdog will not power off
    regardless of input/battery state. It expires on its own; there's no need
    to explicitly cancel unless you want the grace-window protection back
    immediately.
    """
    body: dict[str, Any] = {"minutes": minutes}
    try:
        result = await client.post_json("/control/power/keepalive", body)
    except DaemonError as exc:
        audit.record(tool="set_ups_keepalive", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="set_ups_keepalive", result="ok", payload={**body, "response": result})
    return result


__all__ = ["set_ups_keepalive"]
