"""Session-mutation write tools — metadata, pin/unpin."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


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
    payload = {"session_id": session_id, **body}
    try:
        result = await client.post_json(f"/control/sessions/{session_id}/metadata", body)
    except DaemonError as exc:
        audit.record(
            tool="set_session_metadata",
            result="error",
            payload={**payload, "error": str(exc)},
        )
        raise
    audit.record(tool="set_session_metadata", result="ok", payload={**payload, "response": result})
    return result


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
    payload = {"session_id": session_id, **body}
    try:
        result = await client.post_json(f"/control/sessions/{session_id}/pin", body)
    except DaemonError as exc:
        audit.record(tool="pin_session", result="error", payload={**payload, "error": str(exc)})
        raise
    audit.record(tool="pin_session", result="ok", payload={**payload, "response": result})
    return result


async def unpin_session(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Remove the pin from a session — it's eligible for retention sweeps again."""
    payload = {"session_id": session_id}
    try:
        result = await client.post_json(f"/control/sessions/{session_id}/unpin", {})
    except DaemonError as exc:
        audit.record(tool="unpin_session", result="error", payload={**payload, "error": str(exc)})
        raise
    audit.record(tool="unpin_session", result="ok", payload={**payload, "response": result})
    return result


__all__ = ["set_session_metadata", "pin_session", "unpin_session"]
