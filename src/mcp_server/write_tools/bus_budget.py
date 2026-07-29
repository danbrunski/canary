"""OBD request-rate override write tool — the bounded high-rate capture knob."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def set_bus_budget(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    requests_per_second: float | None,
    ttl_seconds: float | None = 600.0,
) -> dict[str, Any]:
    """Raise (or clear) the OBD request-rate budget for a bounded capture.

    The scheduler paces at ``BUS_BUDGET_MULTIPLIER x poll_rate_hz`` — 8
    requests/second on the dev vehicle — against a measured capability of
    **667 single-PID reads/s**. That conservatism is why a WOT pull yields
    ~23 usable samples where UpRev's K-line logger gets 537, and why AFR and
    Load Point Scaler work has repeatedly had to borrow that external tool.

    Raise it for the pull, drop it after. Pass ``requests_per_second=None``
    to clear and return to the configured default.

    Pairs with ``set_tuning_mode``: narrowing the channel set concentrates
    the budget, raising the budget enlarges it. For WOT work you generally
    want both — but note tuning mode also suspends the Mode-22 DID windows,
    so leave it off if you need DIDs (e.g. ``DID_1208`` injector pulse width)
    during the same drive.

    ``ttl_seconds`` **defaults to 600** rather than to off, unlike
    ``set_passive_mode``. A forgotten passive mode is loud — nothing gets
    logged. A forgotten rate override is quiet: it just keeps hammering the
    bus and burning Pi CPU at up to 25x the intended rate indefinitely. Pass
    ``None`` only if you deliberately want it to persist.

    Ceiling is 200 req/s, well under the measured 667: that figure came from
    a probe with the bus effectively to itself, whereas in service it also
    carries ~29 broadcast IDs on 9-18 ms cadences. Raising the budget also
    stops helping once responses can't keep up — watch ``response_rate`` in
    ``get_status``, and if it drops, the extra requests are just bus noise.
    ``get_status`` reports ``bus_budget`` (override, source, TTL remaining)
    so an unexpectedly high achieved rate is distinguishable from a fault.
    """
    # Sent unconditionally, including as an explicit ``null``. Omitting the
    # key on ``None`` would hand the decision back to the daemon's own
    # ``600.0`` default, making the documented "opt out of the TTL" path
    # unreachable through MCP: the caller asks for no auto-revert and gets a
    # 600 s one, which is the failure mode the TTL exists to prevent —
    # silently reverting mid-capture.
    body: dict[str, Any] = {
        "requests_per_second": requests_per_second,
        "ttl_seconds": ttl_seconds,
    }
    return await audited_post(
        client, audit, tool="set_bus_budget", path="/control/bus-budget", body=body
    )


__all__ = ["set_bus_budget"]
