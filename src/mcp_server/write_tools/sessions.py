"""Session-mutation write tools — metadata, pin/unpin."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def set_session_metadata(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
    metadata: dict[str, Any],
    merge: bool = True,
) -> dict[str, Any]:
    """Attach / merge metadata onto a session.

    ``merge=True`` (default) shallow-merges with existing metadata; ``False``
    replaces. The daemon's REST endpoint uses these same semantics.
    """
    body: dict[str, Any] = {"metadata": metadata, "merge": merge}
    return await audited_post(
        client,
        audit,
        tool="set_session_metadata",
        path=f"/control/sessions/{session_id}/metadata",
        body=body,
        audit_payload={"session_id": session_id, **body},
    )


async def pin_session(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pin a session as a training candidate / interesting-to-keep record.

    ``reason`` and optional ``metadata`` end up in the session's record. Pinned
    sessions survive retention sweeps.
    """
    body: dict[str, Any] = {}
    if reason is not None:
        body["reason"] = reason
    if metadata:
        body["metadata"] = metadata
    return await audited_post(
        client,
        audit,
        tool="pin_session",
        path=f"/control/sessions/{session_id}/pin",
        body=body,
        audit_payload={"session_id": session_id, **body},
    )


async def unpin_session(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Remove the pin from a session — it's eligible for retention sweeps again."""
    return await audited_post(
        client,
        audit,
        tool="unpin_session",
        path=f"/control/sessions/{session_id}/unpin",
        audit_payload={"session_id": session_id},
    )


__all__ = ["set_session_metadata", "pin_session", "unpin_session"]
