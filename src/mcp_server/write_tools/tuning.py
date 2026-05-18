"""Alert-tuning write tools (set, estimate, revert)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def set_tuning(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    config: dict[str, Any],
    change: str = "tuning update",
    by: str | None = None,
) -> dict[str, Any]:
    """Replace the alert-tuning config. The daemon auto-bumps ``version``.

    Run ``estimate_tuning`` first to preview the false-positive impact over
    recent anomalies — the daemon writes every change to an audit log so
    everything is revertible.
    """
    body: dict[str, Any] = {"config": config, "change": change}
    if by is not None:
        body["by"] = by
    try:
        result = await client.post_json("/control/tuning", body)
    except DaemonError as exc:
        audit.record(tool="set_tuning", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="set_tuning", result="ok", payload={**body, "response": result})
    return result


async def estimate_tuning(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    config: dict[str, Any],
    since: str | None = None,
    change: str = "tuning estimate",
    by: str | None = None,
) -> dict[str, Any]:
    """Before/after FP estimate for a proposed config, no state change.

    Audited anyway because seeing what was modeled is useful — the
    ``result="ok"`` entry includes the proposed config and the daemon's
    response.
    """
    body: dict[str, Any] = {"config": config, "change": change}
    if by is not None:
        body["by"] = by
    path = "/control/tuning/estimate"
    if since is not None:
        path = f"{path}?since={since}"
    try:
        result = await client.post_json(path, body)
    except DaemonError as exc:
        audit.record(
            tool="estimate_tuning", result="error", payload={**body, "error": str(exc)}
        )
        raise
    audit.record(tool="estimate_tuning", result="ok", payload={**body, "response": result})
    return result


async def revert_tuning(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    version: int,
    by: str | None = None,
) -> dict[str, Any]:
    """Revert to an earlier tuning version (from the audit log).

    Reverting is itself a new, audited version — the audit chain is
    append-only and never rewinds.
    """
    body: dict[str, Any] = {"version": version}
    if by is not None:
        body["by"] = by
    try:
        result = await client.post_json("/control/tuning/revert", body)
    except DaemonError as exc:
        audit.record(tool="revert_tuning", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="revert_tuning", result="ok", payload={**body, "response": result})
    return result


__all__ = ["set_tuning", "estimate_tuning", "revert_tuning"]
