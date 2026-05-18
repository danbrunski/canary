"""Background-job control write tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def start_job(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    kind: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit a job to the daemon's controller.

    Common ``kind`` values: ``corpus_report``, ``retrain_if``, ``retrain_lstm``.
    See ``src/control/jobs.py`` for the registered set on this build.
    """
    body: dict[str, Any] = {"kind": kind, "params": params or {}}
    try:
        result = await client.post_json("/control/jobs", body)
    except DaemonError as exc:
        audit.record(tool="start_job", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="start_job", result="ok", payload={**body, "response": result})
    return result


async def cancel_job(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    job_id: str,
) -> dict[str, Any]:
    """Best-effort cancellation. Already-finished jobs are a no-op."""
    payload = {"job_id": job_id}
    try:
        result = await client.post_json(f"/control/jobs/{job_id}/cancel", {})
    except DaemonError as exc:
        audit.record(tool="cancel_job", result="error", payload={**payload, "error": str(exc)})
        raise
    audit.record(tool="cancel_job", result="ok", payload={**payload, "response": result})
    return result


__all__ = ["start_job", "cancel_job"]
