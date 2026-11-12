"""End-of-drive digest read tool (Task 5.6, eval-longitudinal-and-dtc.md)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_session_digest(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """End-of-drive digest for one session: duration, mode seconds, gated
    anomaly counts by source, top behavior-surface deviations, fingerprint
    deltas vs. the frozen reference epoch, new DTCs, and new vehicle-ledger
    events.

    Written by the trip-end hook (Task 5.4 — sustained GPS stop, or daemon
    shutdown as a fallback); computed on demand and cached if that hook
    hasn't fired yet for this session. Two distinct failures, both raised as
    :class:`~src.mcp_server.client.DaemonError` — read the status, they mean
    opposite things:

    * **404** — the session id doesn't resolve, or has no telemetry
      recorded. Nothing was attempted.
    * **409** — the digest was *refused*, and the body's ``digest_skipped``
      block says why (D03.6, P02.1): a longer recorded span than
      ``daemon.digest_span_budget_seconds``, a capture with more rows than
      ``daemon.digest_row_budget`` (its tuning section reads raw rows, not
      part by part), a directory that is not a session (``_replay-*``), or a
      child that hit its address-space ceiling. That is a fact about the session, not a
      missing sidecar — do not retry it expecting a different answer.

    A session can hold several trips (D03.63). Each trip end gets its own digest
    or refusal; the answer here is the *newest* trip's, and ``trips`` (on the
    digest, and in the 409 body) lists every trip's status, so an earlier trip's
    digest is visible even when the newest was refused.

    Authenticated (auth-consistency follow-up to Task 5.6): the digest
    aggregates the vehicle ledger and per-session fingerprint z-scores,
    both otherwise only readable via authenticated ``/control/*`` routes
    (``list_vehicle_events`` / ``get_trends``) — ``client`` must carry the
    control token, same as those tools.
    """
    # First hit after a drive computes the digest from the stored session on
    # the Pi (resample + fingerprint + ledger scan) — far beyond the client's
    # 5 s default; cached hits return instantly.
    return await client.get_json(f"/control/sessions/{session_id}/digest", timeout=120.0)


__all__ = ["get_session_digest"]
