"""DTC (diagnostic trouble code) read tool."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_dtc(client: DaemonClient) -> dict[str, Any]:
    """Cached stored/pending/permanent DTC report and MIL state.

    Mirrors ``GET /obd/dtc`` on the dashboard: the periodic Mode 03/07/0A
    poll's latest snapshot, with `description`/`system` per code, `mil_on`,
    `captured_at`, and a `stale` flag when the poll hasn't refreshed recently
    (or no poll has landed yet). Read-only — there is no clear-codes tool.
    """
    return await client.get_json("/obd/dtc")


__all__ = ["get_dtc"]
