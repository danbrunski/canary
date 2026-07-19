"""Feedback write tool — operator/LLM labels on anomaly events.

This is the active-labeling loop from
``docs/roadmap/llm-assist-and-compute-roadmap.md`` exposed as an MCP tool: when the
detector flags an event, Claude can record whether it's a true or false
positive (plus an optional note) and that label flows into the retraining
ledger.
"""

from __future__ import annotations

from typing import Any, Literal

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient, DaemonError

FeedbackLabel = Literal["true_positive", "false_positive", "unknown"]


async def record_feedback(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    anomaly_id: str,
    label: FeedbackLabel,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a verdict on an anomaly event.

    Parameters
    ----------
    anomaly_id:
        Stable identifier of the anomaly (use the ``id`` field on the
        event returned by ``get_recent_anomalies``).
    label:
        ``"true_positive"``, ``"false_positive"``, or ``"unknown"``.
    note:
        Free-text explanation — included in the retraining ledger so future
        triage has context.
    """
    body: dict[str, Any] = {"anomaly_id": anomaly_id, "label": label}
    if note is not None:
        body["note"] = note
    try:
        result = await client.post_json("/control/feedback", body)
    except DaemonError as exc:
        audit.record(tool="record_feedback", result="error", payload={**body, "error": str(exc)})
        raise
    audit.record(tool="record_feedback", result="ok", payload={**body, "response": result})
    return result


__all__ = ["record_feedback", "FeedbackLabel"]
