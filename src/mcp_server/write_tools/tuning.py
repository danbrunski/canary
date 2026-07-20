"""Alert-tuning write tools (set, estimate, revert)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


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
    return await audited_post(client, audit, tool="set_tuning", path="/control/tuning", body=body)


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
    return await audited_post(client, audit, tool="estimate_tuning", path=path, body=body)


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
    return await audited_post(
        client, audit, tool="revert_tuning", path="/control/tuning/revert", body=body
    )


__all__ = ["set_tuning", "estimate_tuning", "revert_tuning"]
