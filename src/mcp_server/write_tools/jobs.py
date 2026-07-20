"""Background-job control write tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


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
    return await audited_post(client, audit, tool="start_job", path="/control/jobs", body=body)


async def cancel_job(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    job_id: str,
) -> dict[str, Any]:
    """Best-effort cancellation. Already-finished jobs are a no-op."""
    return await audited_post(
        client,
        audit,
        tool="cancel_job",
        path=f"/control/jobs/{job_id}/cancel",
        audit_payload={"job_id": job_id},
    )


__all__ = ["start_job", "cancel_job"]
