"""UPS/power read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_power_status(client: DaemonClient) -> dict[str, Any]:
    """UPS (X1202) power state: SoC, cell voltage, watchdog armed/on-battery/
    seconds-on-battery/grace window, and any active keep-alive hold.

    Mirrors ``GET /control/power``. Use this instead of SSH'ing into the Pi to
    check the watchdog when the dashboard is unreachable — a bench Pi shows
    ``on_battery: false`` (external power confirmed); a real key-off shows
    ``seconds_lost`` counting up toward ``grace_s``. ``watchdog_running: false``
    means the ``ups-shutdown`` service isn't up (or its status file is stale —
    check ``watchdog_stale``), not necessarily a power problem.
    """
    return await client.get_json("/control/power")


__all__ = ["get_power_status"]
