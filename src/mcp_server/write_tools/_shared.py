"""Shared helper for write tools: build-body/POST/audit is identical ~15x.

Every write tool in this package does the same three things: POST a JSON
body to the daemon, audit-log a failure (payload + error) if the request
raises, audit-log success (payload + response) otherwise, then return the
response. ``audited_post`` collapses that to one call; call sites keep only
the body-shaping and docstring, which is the part that actually varies.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def audited_post(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    tool: str,
    path: str,
    body: dict[str, Any] | None = None,
    audit_payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST ``body`` to ``path``, audit-logging under ``tool``, and return the response.

    ``audit_payload`` overrides what's recorded (default: ``body``) — for
    call sites where the audit trail should differ from the request body
    (e.g. ``upload_model_artifact`` logs path/size instead of the base64
    blob it actually posts). ``timeout`` overrides the client's default HTTP
    timeout — for a call site whose daemon-side work can legitimately take
    longer than that (e.g. ``obd_request`` waiting out a caller-chosen
    request timeout).
    """
    payload = audit_payload if audit_payload is not None else (body or {})
    try:
        result = await client.post_json(path, body or {}, timeout=timeout)
    except DaemonError as exc:
        audit.record(tool=tool, result="error", payload={**payload, "error": str(exc)})
        raise
    audit.record(tool=tool, result="ok", payload={**payload, "response": result})
    return result


__all__ = ["audited_post"]
