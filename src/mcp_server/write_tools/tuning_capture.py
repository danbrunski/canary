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


async def _rate_verification(
    client: DaemonClient, requested_rate_hz: float | None
) -> dict[str, Any]:
    """Score the running capture against its profile's contract.

    Asks the daemon (``GET /control/capture/quality``) rather than re-deriving
    the rules from ``/status``: the daemon holds the capture-lifetime ledger,
    the arm-time loss epoch and the rows on disk, and it runs the same
    evaluator that writes the sidecar at stop — so the arm-time check and
    the stop-time verdict cannot disagree (C03.9). Until 2026-08-10 they were
    separate implementations, and only the weaker one ran before the drive;
    until C03 the shared one scored a trailing 60 s window.
    """
    quality = await client.get_json("/control/capture/quality")
    quality["requested_rate_hz"] = requested_rate_hz
    return quality


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
    contract and the response carries a ``verdict``: ``pass``, ``degraded``
    (gaps only, every channel >= 90 % covered), ``fail``, or ``unverified``.
    **Read the verdict before driving.** A ``fail`` names the
    clause and the fix in ``rate_verification.failures``; re-arm with the
    correction rather than starting the drive, because none of these problems
    are recoverable afterwards.

    Per-channel ``status`` separates the ways a capture disappoints, because
    they have opposite responses: ``shortfall`` means the achieved cadence
    fell below the requested rate, ``gap`` means the cadence was fine but the
    channel went quiet for a stretch (Mode 22 window closed, engine off, ECU
    stopped answering — see ``coverage.max_gap_s``), and ``missing`` means
    the ECU never answered at all, which wants the channel removed from the
    profile. The verdict names the constraint that actually bound
    (``failing_constraints``: unanswered_input, rate_shortfall,
    acquisition_gap, tuple_incomplete, tuple_skew, persistence_loss,
    unknown_loss_counters) with its evidence; it never prescribes a budget
    or rate change, because an old drop incident is not proof of the
    current limiting resource.

    ``tuples`` is the block to read for a fuelling capture: ``complete`` is
    the number of rounds in which every ``required_tuple`` member was
    persisted within the profile's ``max_tuple_skew_s``, counted from the
    rows on disk. ``tuple_rate_bound_hz`` is only a bound (the slowest
    member's cadence). A capture whose channels all read healthy still
    yields nothing if the tuple never completes.

    The verdict is scoped: ``scope: live`` covers arm to now, on what has
    been persisted so far. Pass ``verify_seconds=0`` to skip the wait and
    return immediately — the verdict is then absent rather than assumed good.
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
        # Only `vehicle_state` is read from it, which the compact view carries
        # for ~1/10th the time and 1/20th the bytes of the full payload.
        status = await client.get_json("/status", {"view": "compact"})
        payload["verified_after_seconds"] = verify_seconds
        verification = await _rate_verification(client, rate_hz)
        payload["rate_verification"] = verification
        # Hoisted to the top level so a caller cannot read the channel table
        # and miss that the contract failed.
        payload["verdict"] = verification.get("verdict")
        payload["scope"] = verification.get("scope")
        payload["failing_constraints"] = [
            item.get("clause") for item in verification.get("failures") or []
        ]
        payload["requested_rate_hz"] = rate_hz
        payload["vehicle_state"] = (status.get("vehicle_state") or {}).get("state")
    return payload


async def get_capture_quality(
    client: DaemonClient,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """The capture contract verdict: live for the active capture, or recomputed.

    Without ``session_id`` this is the running capture scored on what has
    been persisted so far (``scope: live``). With one, the daemon recomputes
    coverage and tuple yield from that session's rows without rewriting it
    and returns the stored sidecar beside the recomputation — a v1 sidecar
    comes back as ``scope: trailing_snapshot`` with losses unknown, which is
    what it was. Read ``failures[].clause`` for the constraint that bound.
    """
    if session_id is None:
        return await client.get_json("/control/capture/quality")
    return await client.get_json(f"/control/capture/quality/{session_id}")


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


__all__ = ["arm_tuning_capture", "disarm_tuning_capture", "get_capture_quality"]
