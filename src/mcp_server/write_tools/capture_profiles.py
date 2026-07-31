"""Start/stop named capture profiles through the daemon."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def start_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    profile: str,
    tune_revision: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"profile": profile}
    if tune_revision is not None:
        body["tune_revision"] = tune_revision
    return await audited_post(
        client, audit, tool="start_capture", path="/control/capture", body=body
    )


async def stop_capture(client: DaemonClient, audit: MCPAuditLogger) -> dict[str, Any]:
    return await audited_post(
        client, audit, tool="stop_capture", path="/control/capture/stop", body={}
    )


__all__ = ["start_capture", "stop_capture"]
