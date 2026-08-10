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

from src.collection.capture_quality import evaluate_capture_quality
from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

#: Long enough to obtain a useful rate sample without making an operator wait
#: through a whole minute.  The daemon reports the actual observed interval.
DEFAULT_VERIFY_SECONDS = 20.0


def _rate_verification(status: dict[str, Any], requested_rate_hz: float | None) -> dict[str, Any]:
    """Score the running capture against its profile's contract.

    Delegates to :func:`src.collection.capture_quality.evaluate_capture_quality`
    rather than re-deriving the rules, so the arm-time check and the sidecar
    written at capture stop cannot disagree about whether a capture is sound.
    They were separate implementations until 2026-08-10, and only the weaker
    one ran before the drive.
    """
    profile = status.get("capture_profile") or {}
    health = status.get("health") or {}
    scheduler = health.get("obd_scheduler") or {}
    storage = (health.get("telemetry_sinks") or {}).get("storage") or {}
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=scheduler,
        storage=storage,
        requested_rate_hz=requested_rate_hz,
    )
    payload = quality.to_dict()
    payload["passive_broadcast_enabled"] = bool(profile.get("broadcast_decode", False))
    return payload


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

    After ``verify_seconds`` the capture is scored against its profile's
    contract and the response carries a ``verdict``: ``pass``, ``fail``, or
    ``unverified``. **Read the verdict before driving.** A ``fail`` names the
    clause and the fix in ``rate_verification.failures``; re-arm with the
    correction rather than starting the drive, because none of these problems
    are recoverable afterwards.

    Per-channel ``status`` separates the ways a capture disappoints, because
    they have opposite fixes: ``shortfall`` means demand exceeded the budget
    (throttling working as designed — lower the rate or raise the budget),
    ``stalled`` means the rate was fine but the channel went quiet for a
    stretch (Mode 22 window closed, engine off, ECU stopped answering) — check
    ``max_gap_seconds`` — and ``missing`` means the ECU never answered at all,
    which wants the channel removed from the profile, not more budget.

    ``tuple_rate_bound_hz`` is the one to look at for a fuelling capture: it
    bounds how often the profile's ``required_tuple`` can complete, and it is
    what per-channel rates cannot tell you. A capture whose channels all read
    healthy still yields nothing if the tuple never completes.

    ``observed_seconds`` is the window the samples were drawn from, not a
    denominator; dividing by it re-creates the conflation that made an
    on-target capture look broken on 2026-08-06. Pass ``verify_seconds=0`` to
    skip the wait and return immediately — the verdict is then absent rather
    than assumed good.
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
        payload["verified_after_seconds"] = verify_seconds
        verification = _rate_verification(status, rate_hz)
        payload["rate_verification"] = verification
        # Hoisted to the top level so a caller cannot read the channel table
        # and miss that the contract failed.
        payload["verdict"] = verification.get("verdict")
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
