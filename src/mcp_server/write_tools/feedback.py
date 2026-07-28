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
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

FeedbackLabel = Literal["true_positive", "false_positive", "unknown"]


async def record_feedback(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    event: dict[str, Any],
    label: FeedbackLabel,
    reason: str | None = None,
    driving_context: dict[str, Any] | None = None,
    labeled_by: str | None = None,
    hypothesis: str | None = None,
    clarifying_question: str | None = None,
    clarifying_answer: str | None = None,
) -> dict[str, Any]:
    """Record a verdict on an anomaly event.

    Takes the *whole event*, not an id. ``FeedbackStore.record`` derives its
    key with ``anomaly_key(event)``, and anomaly events carry no ``id`` field
    at all — they have ``dedupe_key``. This tool previously sent
    ``{anomaly_id, label, note}``, none of which ``FeedbackRequest`` accepts,
    so with ``extra="forbid"`` on the route every call 422'd and the
    active-labeling loop was unreachable over MCP.

    Parameters
    ----------
    event:
        The anomaly event dict, passed through from ``get_recent_anomalies``.
    label:
        ``"true_positive"``, ``"false_positive"``, or ``"unknown"``.
    reason:
        Free-text explanation — included in the retraining ledger so future
        triage has context.
    driving_context:
        What the vehicle was doing, when that isn't evident from the event.
    labeled_by:
        Who/what recorded the verdict.
    hypothesis / clarifying_question / clarifying_answer:
        Task S.3 enrichment. The question/answer pair must be complete —
        supplying one without the other is a 422 from the store.
    """
    body: dict[str, Any] = {"event": event, "label": label}
    if reason is not None:
        body["reason"] = reason
    if driving_context is not None:
        body["driving_context"] = driving_context
    if labeled_by is not None:
        body["labeled_by"] = labeled_by
    if hypothesis is not None:
        body["hypothesis"] = hypothesis
    if clarifying_question is not None:
        body["clarifying_question"] = clarifying_question
    if clarifying_answer is not None:
        body["clarifying_answer"] = clarifying_answer
    return await audited_post(
        client, audit, tool="record_feedback", path="/control/feedback", body=body
    )


__all__ = ["record_feedback", "FeedbackLabel"]
