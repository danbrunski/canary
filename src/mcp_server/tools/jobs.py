"""Background-job read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def list_jobs(client: DaemonClient) -> dict[str, Any]:
    """All known jobs (queued, running, completed, failed)."""
    return await client.get_json("/control/jobs")


async def get_job(client: DaemonClient, *, job_id: str) -> dict[str, Any]:
    """Status + result payload for a single job."""
    return await client.get_json(f"/control/jobs/{job_id}")


async def get_job_logs(client: DaemonClient, *, job_id: str) -> dict[str, Any]:
    """Captured stdout/stderr of a job (full log; the daemon doesn't paginate)."""
    return await client.get_json(f"/control/jobs/{job_id}/logs")


__all__ = ["list_jobs", "get_job", "get_job_logs"]
