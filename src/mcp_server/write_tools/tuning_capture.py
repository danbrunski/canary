"""One-verb capture arming, with verification.

Arming a capture by hand is four HTTP calls that must happen in the right
order — profile, then budget, then wait, then check the achieved rate — and
getting the order wrong is silent. On 2026-08-05 the budget call was rejected
mid-sequence on a TTL bound, so the capture ran for its first minute at the
old rate with nothing saying so.

The daemon owns profile activation and any budget override in one transition.
The MCP tool never follows a successful capture arm with another
state-changing request that could fail independently.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

#: Long enough to obtain a useful rate sample without making an operator wait
#: through a whole minute.  The daemon reports the actual observed interval.
DEFAULT_VERIFY_SECONDS = 20.0


async def arm_tuning_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    profile: str,
    rate_hz: float | None = None,
    bus_budget_requests_per_second: float | None = None,
    ttl_seconds: float | None = 3600.0,
    tune_revision: str | None = None,
    verify_seconds: float = DEFAULT_VERIFY_SECONDS,
) -> dict[str, Any]:
    """Start a capture and optional budget override, then verify the real rate.

    ``rate_hz`` overrides the profile's own target for this capture only —
    the saved profile is untouched, so a one-off fast pull cannot quietly
    become the profile's normal.

    ``bus_budget_requests_per_second`` matters because demand and budget are
    two different knobs: raising the rate without the budget just queues, and
    raising the budget without the rate does *nothing at all* (measured
    2026-08-04 — 8 → 30 req/s moved the achieved rate not at all, because
    per-channel demand was capped at 1 Hz). Set both.

    The response carries capture-scoped ``achieved_hz`` sampled after
    ``verify_seconds`` and its exact ``observed_seconds`` denominator.
    Compare it against what you asked for: a shortfall means demand exceeded
    the budget, which is throttling working as designed rather than a fault.
    Pass ``verify_seconds=0`` to skip the wait and return immediately.
    """
    body: dict[str, Any] = {"profile": profile}
    if rate_hz is not None:
        body["rate_hz"] = rate_hz
    if bus_budget_requests_per_second is not None:
        body["bus_budget_requests_per_second"] = bus_budget_requests_per_second
        body["bus_budget_ttl_seconds"] = ttl_seconds
    if tune_revision is not None:
        body["tune_revision"] = tune_revision
    started = await audited_post(
        client, audit, tool="arm_tuning_capture", path="/control/capture", body=body
    )

    payload: dict[str, Any] = {"capture": started, "bus_budget": started.get("bus_budget")}
    if verify_seconds and verify_seconds > 0:
        await asyncio.sleep(verify_seconds)
        status = await client.get_json("/status")
        scheduler = (status.get("health") or {}).get("obd_scheduler") or {}
        payload["verified_after_seconds"] = verify_seconds
        payload["achieved_hz"] = scheduler.get("achieved_hz_capture")
        payload["observed_seconds"] = scheduler.get("capture_observed_seconds")
        payload["requested_rate_hz"] = rate_hz
        payload["vehicle_state"] = (status.get("vehicle_state") or {}).get("state")
    return payload


async def disarm_tuning_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    reason: str | None = None,
    clear_bus_budget: bool = False,
) -> dict[str, Any]:
    """Stop the active capture and put the bus budget back.

    Stopping restores exactly the override that was in force before capture.
    ``clear_bus_budget`` is an explicit request to discard that restored
    operator setting. The response includes the ended session directory.
    """
    body: dict[str, Any] = {}
    if reason is not None:
        body["reason"] = reason
    stopped = await audited_post(
        client, audit, tool="disarm_tuning_capture", path="/control/capture/stop", body=body
    )
    budget: dict[str, Any] | None = None
    if clear_bus_budget:
        budget = await audited_post(
            client,
            audit,
            tool="disarm_tuning_capture",
            path="/control/bus-budget",
            body={"requests_per_second": None},
        )
    return {"capture": stopped, "bus_budget": budget}


__all__ = ["arm_tuning_capture", "disarm_tuning_capture"]
