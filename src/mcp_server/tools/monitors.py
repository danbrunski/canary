"""Mode 06 on-board monitor (misfire counts + test margins) read tool."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_monitors(client: DaemonClient) -> dict[str, Any]:
    """Cached Mode 06 on-board monitor sweep: per-cylinder misfire counts and
    raw monitor test results (value/min/max, pass/fail).

    Mirrors ``GET /obd/monitors`` on the dashboard: the periodic Mode 06
    poll's latest snapshot (Task 3.1), with a `stale` flag when the poll
    hasn't refreshed recently (or no poll has landed yet). Read-only.
    """
    return await client.get_json("/obd/monitors")


__all__ = ["get_monitors"]
