"""UPS/power read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_power_status(client: DaemonClient) -> dict[str, Any]:
    """UPS (X1202) power state: SoC, cell voltage, the watchdog's conditions and
    its shutdown countdown, and any active keep-alive hold.

    Mirrors ``GET /control/power``. Use this instead of SSH'ing into the Pi to
    check the watchdog when the dashboard is unreachable.

    Read the conditions separately, they are independent measurements:
    ``input_lost`` (the PLD line only), ``discharging`` (SoC falling faster than
    ``min_discharge_rate_pct_per_hour``; ``discharge_rate_pct_per_hour`` is the
    measured slope, and it can be true with input present on a sagging supply)
    and ``soc_critical`` (SoC at or below ``critical_soc_pct``, true while
    charging too). ``shutdown_state`` says what the countdown is doing:
    ``input_present``, ``counting_down`` (``seconds_lost`` toward ``grace_s``),
    ``held_external_power`` (input lost but the battery is flat, a bench Pi),
    ``held_keepalive`` (a hold is overriding a countdown, even at a critical
    SoC) or ``disarmed``; ``countdown_reason`` names why it counts
    (``input_lost_discharging``, ``input_lost_soc_critical``,
    ``input_lost_unconfirmed``). ``on_battery`` is a legacy alias, true only
    when ``input_lost`` and the battery is draining or critical. The new fields
    are null on a watchdog older than D03.67 until it is redeployed.
    ``watchdog_running: false`` means the ``ups-shutdown`` service isn't up (or
    its status file is stale, check ``watchdog_stale``), not necessarily a
    power problem.
    """
    return await client.get_json("/control/power")


__all__ = ["get_power_status"]
