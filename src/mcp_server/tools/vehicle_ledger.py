"""Vehicle maintenance/intervention ledger read tool (Task 3.3)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def list_vehicle_events(client: DaemonClient) -> dict[str, Any]:
    """Every vehicle ledger entry, oldest-first (latest per id — corrections
    collapsed, but nothing lost from the on-disk append-only chain).

    Use this to check what's already logged before recording a duplicate, or
    to correlate an anomaly's timestamp against nearby maintenance/mods.
    """
    return await client.get_json("/control/vehicle-ledger")


__all__ = ["list_vehicle_events"]
