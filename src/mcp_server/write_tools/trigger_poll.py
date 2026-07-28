"""Force-poll on demand (``trigger_poll`` tool)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def trigger_poll(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    channels: list[str],
) -> dict[str, Any]:
    """Force specific channels due on the OBD scheduler's next tick.

    Nudges the poll scheduler instead of waiting out a channel's configured
    cadence — DTC/Mode 06 sit on a 5-minute tier, so this turns "wait up to
    5 minutes" into "the answer is ready in a few seconds." Channel names
    match `get_status`'s `obd_scheduler.requests[].channel` (`RPM`,
    `DTC_STORED`, `MONITOR_RESULTS`, `DID_1101`, ...), matched
    case-insensitively; an unknown name is silently never matched.

    This only queues the request — it does not wait for or return the
    reading. Read the result back a few seconds later with the usual tools
    (`get_status`, `get_dtc`, `get_recent_telemetry`, `query_telemetry`).
    """
    return await audited_post(
        client,
        audit,
        tool="trigger_poll",
        path="/control/trigger_poll",
        body={"channels": channels},
    )


__all__ = ["trigger_poll"]
