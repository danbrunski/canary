"""One-verb capture arming, with verification (roadmap Phase 3.3).

Arming a capture by hand is four HTTP calls that must happen in the right
order — profile, then budget, then wait, then check the achieved rate — and
getting the order wrong is silent. On 2026-08-05 the budget call was rejected
mid-sequence on a TTL bound, so the capture ran for its first minute at the
old rate with nothing saying so.

These wrap the existing ``/control/capture`` and ``/control/bus-budget``
routes; no new daemon state. The point is that arming *reports what it
actually achieved* rather than what it asked for.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

#: How long to wait before reading the achieved rate back. The rate metric is
#: a 60 s trailing window, so a shorter wait under-reports; this is the
#: shortest delay that gives a usable signal without stalling the caller.
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
    """Start a capture profile, raise the budget, then verify the real rate.

    ``rate_hz`` overrides the profile's own target for this capture only —
    the saved profile is untouched, so a one-off fast pull cannot quietly
    become the profile's normal.

    ``bus_budget_requests_per_second`` matters because demand and budget are
    two different knobs: raising the rate without the budget just queues, and
    raising the budget without the rate does *nothing at all* (measured
    2026-08-04 — 8 → 30 req/s moved the achieved rate not at all, because
    per-channel demand was capped at 1 Hz). Set both.

    The response carries ``achieved_hz_60s`` sampled after ``verify_seconds``.
    Compare it against what you asked for: a shortfall means demand exceeded
    the budget, which is throttling working as designed rather than a fault.
    Pass ``verify_seconds=0`` to skip the wait and return immediately.
    """
    body: dict[str, Any] = {"profile": profile}
    if rate_hz is not None:
        body["rate_hz"] = rate_hz
    if tune_revision is not None:
        body["tune_revision"] = tune_revision
    started = await audited_post(
        client, audit, tool="arm_tuning_capture", path="/control/capture", body=body
    )

    budget: dict[str, Any] | None = None
    if bus_budget_requests_per_second is not None:
        budget = await audited_post(
            client,
            audit,
            tool="arm_tuning_capture",
            path="/control/bus-budget",
            body={
                "requests_per_second": bus_budget_requests_per_second,
                "ttl_seconds": ttl_seconds,
            },
        )

    payload: dict[str, Any] = {"capture": started, "bus_budget": budget}
    if verify_seconds and verify_seconds > 0:
        await asyncio.sleep(verify_seconds)
        status = await client.get_json("/status")
        scheduler = (status.get("health") or {}).get("obd_scheduler") or {}
        payload["verified_after_seconds"] = verify_seconds
        payload["achieved_hz_60s"] = scheduler.get("achieved_hz_60s")
        payload["requested_rate_hz"] = rate_hz
        payload["vehicle_state"] = (status.get("vehicle_state") or {}).get("state")
    return payload


async def disarm_tuning_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    reason: str | None = None,
    clear_bus_budget: bool = True,
) -> dict[str, Any]:
    """Stop the active capture and put the bus budget back.

    Stopping the capture already restores whatever override was in force
    before it started; ``clear_bus_budget`` additionally drops any override
    the operator set by hand, which is the usual intent after a pull. The
    response includes the session directory so the capture can be analysed
    without hunting for it.
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
