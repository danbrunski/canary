"""Write tool: trigger session classification on demand.

The classifier itself is idempotent and side-effect-bounded (writes one
``classification.json`` + one ``derived_tags.jsonl`` inside the session
directory). Audit-logging the trigger keeps "Claude relabeled X
sessions" distinguishable from operator-driven runs of
``scripts/label_sessions.py``.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError


async def classify_session(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Run the classifier on ``session_id`` and persist the sidecar.

    Returns the new classification payload (same shape as
    :func:`get_session_classification`). Re-running on a session with
    an existing sidecar overwrites — the classifier is deterministic
    given the same inputs, so re-runs are safe and useful when the
    classifier_version has been bumped.
    """
    body: dict[str, Any] = {"session_id": session_id}
    try:
        result = await client.post_json(
            f"/control/sessions/{session_id}/classification",
            {},
        )
    except DaemonError as exc:
        audit.record(
            tool="classify_session",
            result="error",
            payload={**body, "error": str(exc)},
        )
        raise
    audit.record(
        tool="classify_session",
        result="ok",
        payload={**body, "disposition": result.get("classification", {}).get("disposition")},
    )
    return result


__all__ = ["classify_session"]
